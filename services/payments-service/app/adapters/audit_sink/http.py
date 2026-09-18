"""HTTP sink — POST the event to audit-service's ingest and keep the receipt.

stdlib urllib, matching the other internal clients: no extra runtime dependency and a leaner image.
The URL comes from CONFIG only, never from a request, so no caller can redirect the audit trail at
a host of their choosing — which would be a neat way to make an action look witnessed while the
evidence went somewhere the operator cannot read.

**It RETRIES a transient failure, SAFELY.** Every emit carries an idempotency_key, generated once
per action and reused across this call's retries. The ingest recognises a repeat: it returns the
ORIGINAL seq/hash and appends nothing, so a retry can never add a duplicate to the tamper-evident
chain. That dissolves the objection this sink used to carry ("a retry cannot be told from a second
action") — with the key it can, so a single slow log call no longer fails the real user action it
was witnessing. Retries cover only the RETRYABLE failures: a connection reset, a timeout (the append
may or may not have landed — the key makes either outcome safe), and a 503 (audit busy, or a
reservation still in flight). A 4xx is the caller's own bad request and is never retried.

**It does not raise.** A logging problem must never fail the action it was witnessing. The caller
gets FAILED and the failure policy decides whether the action may proceed unwitnessed.
"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
import uuid

from app.ports.audit_sink import ACCEPTED, FAILED, AuditEvent, AuditReceipt


def _fetch(req, timeout) -> tuple[int, bytes]:
    """The BLOCKING part (urlopen + read), isolated so emit() can run it off the event loop via
    asyncio.to_thread. Run inline on the loop it froze /healthz for the whole round-trip on every
    audited action — the crash-loop this fixes."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - config-only host
        return resp.status, resp.read()


class HttpAuditSink:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 5, *, max_attempts: int = 2,
                 backoff: float = 0.2) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = max(0.0, float(backoff))

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        payload = event.to_payload()
        # ONE key per action, reused across THIS call's retries. A retried append is recognised by
        # the log (original receipt, nothing appended); a first append that never landed appends
        # once. Either way, exactly one event — which is what makes retrying a timeout safe at all.
        payload["idempotency_key"] = uuid.uuid4().hex
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        # ADD-ONLY verifiable caller identity (aud=audit-service) when this service signs its outbound
        # internal calls; the token above is untouched. Both lookups are cached, so this is cheap even
        # on the audit hot path.
        from app.config import get_settings
        from app.core.internal_client import assertion_headers
        headers.update(assertion_headers(get_settings(), target="audit-service"))
        req = urllib.request.Request(self._url, data=body, method="POST", headers=headers)

        last = "audit-service unreachable"
        for attempt in range(self._max_attempts):
            retryable = False
            try:
                # OFF the event loop: a blocking urlopen here froze /healthz for the whole HTTP
                # round-trip on every audited action, failing the liveness probe under normal load.
                status, raw = await asyncio.to_thread(_fetch, req, self._timeout)
            except urllib.error.HTTPError as exc:
                # A definitive HTTP response. 503 (audit busy / a reservation in flight) is worth
                # another attempt with the same key; a 4xx is the caller's fault and is not.
                if exc.code == 503:
                    last, retryable = "audit ingest returned HTTP 503", True
                else:
                    return AuditReceipt(status=FAILED, detail=f"audit ingest returned HTTP {exc.code}")
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                # Connection reset or timeout — the append MAY have landed; the idempotency key makes
                # a retry safe either way.
                last, retryable = f"audit-service unreachable: {exc}", True
            else:
                if not 200 <= status < 300:
                    return AuditReceipt(status=FAILED, detail=f"audit ingest returned HTTP {status}")
                try:
                    receipt = json.loads(raw or b"{}")
                except (ValueError, TypeError):
                    # 200 with a body we cannot read is not proof of anything. The seq and hash ARE
                    # the evidence of where this sits in the chain; without them we cannot claim it.
                    return AuditReceipt(status=FAILED, detail=f"HTTP {status} with an unreadable receipt")
                if not isinstance(receipt, dict) or not receipt.get("hash"):
                    return AuditReceipt(status=FAILED,
                                        detail=f"HTTP {status} but no chain hash — nothing proves it landed")
                return AuditReceipt(status=ACCEPTED, id=str(receipt.get("id", "")),
                                    seq=int(receipt.get("seq") or 0), hash=str(receipt["hash"]))
            if not (retryable and attempt + 1 < self._max_attempts):
                break
            await asyncio.sleep(self._backoff)
        return AuditReceipt(status=FAILED,
                            detail=f"{last} (after {self._max_attempts} attempt(s))")
