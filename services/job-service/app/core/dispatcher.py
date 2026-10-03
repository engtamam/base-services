"""Push dispatch — deliver a queue's due jobs to a registered webhook, on the durable queue's rails.

The queue is PULL by default: a worker calls claim(). Push is an OPT-IN layer on top, per queue: an
operator registers a webhook for a queue, and job-service then delivers that queue's due jobs by
POSTing each one to the webhook — so a consuming service no longer needs its own CronJob to poll
claim(). One central ticker (the scheduler's own) drives everything.

The delivery is NOT a new code path with its own retry logic. It is a built-in WORKER: dispatch_batch
CLAIMS jobs through the exact same atomic lease (CAS + fencing token), POSTs them, and then acks on a
2xx or nacks on anything else. So every durability guarantee is the queue's existing one, reused, not
reimplemented:
  • no job lost         — a failed POST is a nack → the queue's own backoff/retry, then DLQ.
  • no double-dispatch  — the atomic claim leases each job to exactly ONE dispatcher (many job-service
                          pods can run this concurrently; the CAS decides the winner).
  • no cross-tenant mix — each claimed job is POSTed individually carrying ONLY its own tenant.
  • idempotent fire     — unchanged: the scheduler's per-fire unique_key still gates enqueue.

Security:
  • SSRF (CWE-918): a target url's host must be in the egress allowlist, checked at REGISTER time, so a
    target can never be pointed at a host of someone's choosing.
  • Provenance: every POST carries the shared internal token (verified by the receiver today) + an
    RS256 caller assertion (issuer=job-service, when signing is configured) + an HMAC signature over
    the exact body and a timestamp (integrity + replay window). See core/push_signing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from app.core import push_signing
from app.core.models import DispatchTarget

_log = logging.getLogger(__name__)

# (status, ok) — a poster returns the HTTP status, or raises on a transport error (treated as failure).
Poster = Callable[[str, bytes, dict[str, str], float], Awaitable[int]]


class DispatchError(ValueError):
    """A dispatch target the service refuses to register (bad url, host not allow-listed)."""


def _default_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    with urllib.request.urlopen(                                   # nosec B310 - host is allow-listed
            urllib.request.Request(url, data=body, method="POST", headers=headers),
            timeout=timeout) as resp:
        resp.read()
        return int(resp.status)


async def _post_off_loop(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    """The default poster: a blocking urllib POST run OFF the event loop (a sync urlopen on the loop
    froze /healthz for the whole round-trip — see the audit-sink fix). HTTPError carries a status we
    still want (4xx/5xx = a definite non-2xx failure); other errors propagate as a transport failure."""
    try:
        return await asyncio.to_thread(_default_post, url, body, headers, timeout)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


class Dispatcher:
    def __init__(self, *, targets, jobs, clock, config: dict[str, Any],
                 poster: Poster | None = None, headers_for=None) -> None:
        self._targets = targets                        # DispatchTargetStore
        self._jobs = jobs                              # JobService (claim/ack/nack)
        self._clock = clock
        self._cfg = config
        self._post = poster or _post_off_loop
        # How the outbound token + RS256 assertion headers are built for a target host. Injected so a
        # test need not stand up the settings/assertion machinery; defaults to the real internal client.
        self._headers_for = headers_for or self._default_headers_for

    def _default_headers_for(self, host: str) -> dict[str, str]:
        from app.config import get_settings
        from app.core.internal_client import internal_headers
        return internal_headers(get_settings(), target=host)

    # ── target registry (config, low volume, operator/internal-driven) ──────────
    def _validate_url(self, url: str) -> str:
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https"):
            raise DispatchError("a dispatch url must be http(s)")
        host = (parts.hostname or "").lower()
        if not host:
            raise DispatchError("a dispatch url has no host")
        allowed = self._cfg["allowed_hosts"]
        if host not in allowed:
            # Fail closed: an empty allowlist allows NOTHING. A webhook host must be declared in
            # EGRESS_ALLOWED_HOSTS before a target can point at it (SSRF / impersonation-target guard).
            raise DispatchError(f"dispatch host not allow-listed: {host}")
        return url

    async def register(self, *, queue: str, url: str, active: bool = True) -> DispatchTarget:
        if not queue:
            raise DispatchError("a dispatch target needs a queue")
        self._validate_url(url)
        now = self._clock.now()
        existing = await self._targets.get(queue)
        t = DispatchTarget(queue=queue, url=url, active=active,
                           created=existing.created if existing else now, updated=now)
        return await self._targets.upsert(t)

    async def unregister(self, queue: str) -> bool:
        return await self._targets.delete(queue)

    async def list_targets(self) -> list[dict]:
        return await self._targets.list()

    # ── the push worker — claim due jobs on push queues, deliver, ack/nack ──────
    async def dispatch_batch(self) -> dict[str, Any]:
        """One pass: for every active push queue, claim a batch of due jobs and deliver each. Driven
        by the same central ticker that calls scheduler/tick and jobs/reclaim. A no-op (never an error)
        when push is disabled, so enabling it is purely additive."""
        if not self._cfg.get("enabled"):
            return {"enabled": False, "dispatched": 0, "failed": 0}
        # job-006: deliver with BOUNDED CONCURRENCY. Strictly sequential delivery let one slow receiver
        # head-of-line-block every other push queue for the whole pass. A semaphore caps the in-flight
        # POSTs (so a stuck receiver cannot exhaust the loop) while letting healthy targets proceed.
        sem = asyncio.Semaphore(max(1, int(self._cfg.get("concurrency", 8))))
        skipped = 0

        async def _deliver(target, job):
            async with sem:
                return await self._deliver_one(target, job)

        tasks: list[Awaitable[bool]] = []
        for target in await self._targets.active():
            # job-014: re-validate the target host against the allowlist AT DELIVERY, not only at register
            # time — EGRESS_ALLOWED_HOSTS may have tightened since, and a now-forbidden host must not be
            # POSTed to. Skip it (leaving its jobs for a re-registered/clean target) rather than deliver.
            try:
                self._validate_url(target.url)
            except DispatchError:
                _log.warning("push_target_not_allowlisted",
                             extra={"queue": target.queue,
                                    "host": (urlsplit(target.url).hostname or "").lower()})
                skipped += 1
                continue
            claimed = await self._jobs.claim(
                worker=f"push:{target.queue}", queue=target.queue, batch=self._cfg["batch"])
            tasks.extend(_deliver(target, job) for job in claimed)
        results = await asyncio.gather(*tasks) if tasks else []
        dispatched = sum(1 for r in results if r)
        failed = sum(1 for r in results if not r)
        return {"enabled": True, "dispatched": dispatched, "failed": failed,
                "skipped_targets": skipped}

    async def _deliver_one(self, target: DispatchTarget, job: dict[str, Any]) -> bool:
        job_id = job.get("id", "")
        lease_token = job.get("lease_token", "")
        # NEVER send the fencing lease_token to the receiver — it is a worker secret this dispatcher
        # holds to ack/nack. The receiver gets the job WITHOUT it (one tenant, its own).
        outbound = {k: v for k, v in job.items() if k != "lease_token"}
        body = json.dumps({"job": outbound}, separators=(",", ":")).encode("utf-8")
        host = (urlsplit(target.url).hostname or "").lower()
        ts = int(self._clock.now())
        headers = {"Content-Type": "application/json", **self._headers_for(host)}
        sig = push_signing.sign(self._cfg["signing_secret"], ts, body)
        if sig:
            headers[push_signing.SIGNATURE_HEADER] = sig
            headers[push_signing.TIMESTAMP_HEADER] = str(ts)
        # Renew the lease immediately BEFORE the POST (job-005). A whole batch is claimed at once and
        # delivered one at a time, each POST up to the push timeout; without this the lease of a job
        # late in the batch expires mid-POST and a concurrent reclaim re-queues it, double-delivering.
        # A heartbeat resets the visibility window to now+lease for each job as it is sent.
        if lease_token:
            try:
                await self._jobs.heartbeat(job_id, lease_token)
            except Exception:   # noqa: BLE001 — a heartbeat failure must not abort delivery
                pass
        try:
            status = await self._post(target.url, body, headers, float(self._cfg["timeout"]))
        except Exception as exc:  # noqa: BLE001 — any transport error is a delivery FAILURE, never a crash
            _log.warning("push_delivery_error",
                         extra={"queue": target.queue, "job": job_id, "error": type(exc).__name__})
            await self._jobs.nack(job_id, lease_token, error=f"push transport error: {type(exc).__name__}")
            return False
        if 200 <= status < 300:
            await self._jobs.ack(job_id, lease_token, result={"push_status": status})
            return True
        # Any non-2xx (the receiver was down, busy, or rejected) → the queue's own retry/backoff/DLQ.
        await self._jobs.nack(job_id, lease_token, error=f"push HTTP {status}")
        return False
