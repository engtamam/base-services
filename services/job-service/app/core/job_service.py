"""Core job queue — durable enqueue, atomic lease-based claim, retry, DLQ, reclaim.

Correctness guarantees:
  • **no double-execution** — a claim atomically moves a job queued→leased and removes it
    from the claimable index; every ack/nack/heartbeat carries a **fencing token** (the
    lease token), so a worker that lost its lease (it expired and someone reclaimed) can
    never mutate the job it no longer owns.
  • **no lost jobs** — a claimed job has a lease with an expiry; if the worker dies without
    acking, `reclaim()` returns it to the queue (visibility timeout). Bounded by
    max_attempts, then dead-letter — never an infinite crash loop.
  • **idempotent / unique enqueue** — a retried enqueue (same idempotency_key) or a second
    identical pending job (same unique_key) returns the existing job, not a duplicate.
  • **hot path** — enqueue writes the job + one index entry (no scan); claim reads the
    small due-index (bounded by batch), never the whole job table.
"""
from __future__ import annotations

from typing import Any

from app.core import backoff as _backoff
from app.core.models import (ACTIVE, CANCELED, DEAD, DONE, LEASED, QUEUED, Job, new_id)
from app.core.stores import JobStore


class JobService:
    def __init__(self, *, jobs: JobStore, clock, config: dict[str, Any], event_bus=None) -> None:
        self._jobs = jobs
        self._clock = clock
        self._cfg = config
        self._bus = event_bus

    # ── enqueue (hot path) ──────────────────────────────────────────────────────
    async def enqueue(self, *, queue: str = "default", tenant: str = "", type: str = "",
                      payload: dict | None = None, priority: int = 0, delay_seconds: float = 0,
                      max_attempts: int | None = None, unique_key: str = "",
                      idempotency_key: str = "", schedule_id: str = "") -> tuple[Job, bool]:
        """Returns (job, created?). created=False when an idempotency/unique key matched."""
        now = self._clock.now()
        ttl = self._cfg["unique_ttl"]
        # idempotent retry → the same job, no duplicate
        for key in (idempotency_key, unique_key):
            if key:
                existing_id = await self._jobs.unique_get(key, now=now, ttl=ttl)
                if existing_id:
                    existing = await self._jobs.get(existing_id)
                    if existing and (key == idempotency_key or existing.status in ACTIVE):
                        return existing, False

        job = Job(id=new_id("job_"), queue=queue, tenant=tenant, type=type, payload=payload or {},
                  priority=priority, status=QUEUED, next_run_at=now + max(0.0, delay_seconds),
                  max_attempts=max_attempts or self._cfg["retry_max_attempts"],
                  unique_key=unique_key, idempotency_key=idempotency_key,
                  schedule_id=schedule_id, created=now, updated=now)
        # ATOMICALLY reserve the keys BEFORE creating the job. The old unique_get->unique_put pair had
        # a gap where two concurrent same-key enqueues both saw "absent" and both created a job. If a
        # reserve loses, another enqueue won the key concurrently — return that job (or, for a
        # non-active unique match, fall through as the existing logic allowed).
        for key in (idempotency_key, unique_key):
            if key and not await self._jobs.unique_reserve(key, job.id, now, ttl=ttl):
                existing_id = await self._jobs.unique_get(key, now=now, ttl=ttl)
                existing = await self._jobs.get(existing_id) if existing_id else None
                if existing and (key == idempotency_key or existing.status in ACTIVE):
                    return existing, False
        await self._jobs.save(job)
        await self._jobs.index_due(job)                       # 1 index write — no scan
        await self._emit("job.enqueued", job)
        return job, True

    # ── claim (hot path) — atomic lease with fencing token ──────────────────────
    async def claim(self, *, worker: str, queue: str | None = None, batch: int = 1) -> list[dict]:
        batch = max(1, min(int(batch), self._cfg["claim_batch_max"]))
        now = self._clock.now()
        # BOUNDED oldest-first page off the next_run_at index — not a scan+sort of the whole due-index
        # (job-r2-03). A window a few multiples of the batch leaves room after the due/queue filter.
        window = min(self._cfg["claim_batch_max"], max(batch * 8, 100))
        candidates = [d for d in await self._jobs.due_page(limit=window)
                      if float(d.get("next_run_at", 0)) <= now
                      and (queue is None or d.get("queue") == queue)]
        candidates = self._order(candidates)                  # priority + tenant fairness

        claimed: list[dict] = []
        for cand in candidates:
            if len(claimed) >= batch:
                break
            job = await self._jobs.get(cand.get("id", ""))
            if job is None or job.status != QUEUED or job.next_run_at > now:
                await self._jobs.unindex_due(cand.get("id", ""))   # stale index entry
                continue
            expected = int(job.version)
            job.status = LEASED
            job.lease_token = new_id("lt_")
            job.lease_expires = now + self._cfg["lease_seconds"]
            job.updated = now
            # ATOMIC QUEUED->LEASED: only the CAS winner leases. Without it two workers both read the
            # job QUEUED and both saved a lease (double-execution) — the exact guarantee the docstring
            # makes. A loser skips this candidate; the job is already leased by the winner.
            if not await self._jobs.cas_save(job, expected):
                continue
            await self._jobs.unindex_due(job.id)              # invisible while leased
            await self._jobs.index_leased(job)
            claimed.append({**job.to_dict()})                 # worker needs the lease_token
        if claimed:
            await self._emit("job.claimed", None, {"worker": worker, "count": len(claimed)})
        return claimed

    def _order(self, candidates: list[dict]) -> list[dict]:
        candidates.sort(key=lambda d: (-int(d.get("priority", 0)), float(d.get("next_run_at", 0))))
        if not self._cfg.get("fairness", True):
            return candidates
        # round-robin across tenants WITHIN each priority tier — one tenant's flood of
        # same-priority jobs can't starve another's.
        seen: dict[tuple, int] = {}
        for d in candidates:
            tier = (-int(d.get("priority", 0)), d.get("tenant", ""))
            d["_rr"] = seen[tier] = seen.get(tier, -1) + 1
        candidates.sort(key=lambda d: (-int(d.get("priority", 0)), d.get("_rr", 0),
                                       float(d.get("next_run_at", 0))))
        return candidates

    # ── ack / nack / heartbeat (fencing-token guarded) ──────────────────────────
    def _held(self, job: Job | None, token: str) -> bool:
        return job is not None and job.status == LEASED and job.lease_token == token

    async def ack(self, job_id: str, lease_token: str, result: dict | None = None) -> dict:
        job = await self._jobs.get(job_id)
        if not self._held(job, lease_token):
            return {"ok": False, "reason": "not_leased_or_stale_token"}   # fencing
        job.status, job.result, job.updated = DONE, result or {}, self._clock.now()
        job.lease_token = ""
        await self._jobs.save(job)
        await self._jobs.unindex_leased(job.id)
        await self._jobs.unique_clear(job.unique_key)          # a done job frees its unique slot
        await self._emit("job.done", job)
        return {"ok": True, "status": DONE}

    async def nack(self, job_id: str, lease_token: str, error: str = "") -> dict:
        job = await self._jobs.get(job_id)
        if not self._held(job, lease_token):
            return {"ok": False, "reason": "not_leased_or_stale_token"}
        return await self._fail(job, error or "nack")

    async def _fail(self, job: Job, error: str) -> dict:
        now = self._clock.now()
        job.attempt += 1
        job.error = error
        job.lease_token = ""
        await self._jobs.unindex_leased(job.id)
        delay = _backoff.next_delay(job.attempt, base_seconds=self._cfg["retry_base_seconds"],
                                    max_seconds=self._cfg["retry_max_seconds"],
                                    max_attempts=job.max_attempts, seed=job.id,
                                    jitter=self._cfg["retry_jitter"])
        if delay is None:                                      # exhausted → dead-letter, never dropped
            job.status, job.updated = DEAD, now
            await self._jobs.save(job)
            await self._emit("job.dead_lettered", job)
            return {"ok": True, "status": DEAD, "attempt": job.attempt}
        job.status, job.next_run_at, job.updated = QUEUED, now + delay, now
        await self._jobs.save(job)
        await self._jobs.index_due(job)
        return {"ok": True, "status": QUEUED, "attempt": job.attempt, "retry_in": round(delay, 2)}

    async def heartbeat(self, job_id: str, lease_token: str) -> dict:
        job = await self._jobs.get(job_id)
        if not self._held(job, lease_token):
            return {"ok": False, "reason": "not_leased_or_stale_token"}
        job.lease_expires = self._clock.now() + self._cfg["heartbeat_extend"]
        await self._jobs.save(job)
        await self._jobs.index_leased(job)
        return {"ok": True, "lease_expires": job.lease_expires}

    # ── reclaim (visibility timeout — a crashed worker's job returns) ───────────
    async def reclaim(self) -> dict:
        now = self._clock.now()
        reclaimed, dead = 0, 0
        for row in await self._jobs.leased_index():
            if float(row.get("lease_expires", 0)) > now:
                continue
            job = await self._jobs.get(row.get("job_id", ""))
            if job is None or job.status != LEASED or job.lease_expires > now:
                await self._jobs.unindex_leased(row.get("job_id", ""))
                continue
            # A crashed worker: make the job claimable AGAIN IMMEDIATELY (visibility-timeout
            # expiry, like SQS) — but count the attempt, so a job that keeps crashing workers
            # still reaches the dead-letter queue instead of looping forever.
            expected = int(job.version)
            job.attempt += 1
            job.error = "lease_expired"
            job.lease_token = ""
            if job.attempt >= job.max_attempts:
                job.status, job.updated = DEAD, now
            else:
                job.status, job.next_run_at, job.updated = QUEUED, now, now  # immediately claimable
            # ATOMIC LEASED->QUEUED/DEAD. The old blind save could RESURRECT a job a concurrent
            # ack/nack/cancel had already finished (re-execution — job-r2-01): between the get above and
            # the write, that op moved the job out of LEASED, and the blind save overwrote it. The CAS
            # (expected version) fails if anything changed, so the reaper skips a job that is no longer
            # the stale-leased one it read.
            if not await self._jobs.cas_save(job, expected):
                continue
            await self._jobs.unindex_leased(job.id)
            if job.status == DEAD:
                await self._emit("job.dead_lettered", job)
                dead += 1
            else:
                await self._jobs.index_due(job)
                reclaimed += 1
        return {"reclaimed": reclaimed, "dead_lettered": dead}

    # ── admin ────────────────────────────────────────────────────────────────────
    @staticmethod
    def _owns(job: "Job | None", tenant: str | None, is_operator: bool) -> bool:
        """A tenant-scoped caller may only touch a job in its OWN tenant; a platform operator any.
        Guards the by-id admin actions (replay/cancel) against a cross-tenant IDOR, matching the
        tenant check the read path already applies."""
        return job is not None and (is_operator or job.tenant == (tenant or "__none__"))

    async def cancel(self, job_id: str, *, tenant: str | None = None,
                     is_operator: bool = True) -> dict:
        job = await self._jobs.get(job_id)
        if not self._owns(job, tenant, is_operator):
            return {"ok": False, "reason": "not_found"}     # cross-tenant/absent: never leak, never act
        if job.status not in ACTIVE:
            return {"ok": False, "reason": "not_cancelable"}
        job.status, job.updated = CANCELED, self._clock.now()
        await self._jobs.save(job)
        await self._jobs.unindex_due(job.id)
        await self._jobs.unindex_leased(job.id)
        await self._jobs.unique_clear(job.unique_key)
        return {"ok": True, "status": CANCELED}

    async def replay(self, job_id: str, *, tenant: str | None = None,
                     is_operator: bool = True) -> dict:
        job = await self._jobs.get(job_id)
        if not self._owns(job, tenant, is_operator):
            return {"ok": False, "reason": "not_found"}     # cross-tenant/absent: never leak, never act
        if job.status != DEAD:
            return {"ok": False, "reason": "not_dead"}
        now = self._clock.now()
        job.status, job.attempt, job.error, job.next_run_at, job.updated = QUEUED, 0, "", now, now
        await self._jobs.save(job)
        await self._jobs.index_due(job)
        return {"ok": True, "status": QUEUED}

    async def get(self, job_id: str) -> Job | None:
        return await self._jobs.get(job_id)

    async def stats(self, *, tenant: str | None = None) -> dict[str, Any]:
        # tenant=None is the platform-operator view (fleet-wide); a tenant caller sees ONLY its own
        # counts — otherwise /stats leaked global cross-tenant job counts to any jobs.read holder.
        counts = await self._jobs.counts(tenant=tenant)
        # BOUNDED due page (indexed), tenant-scoped when applicable — not a whole due-index scan.
        due = await self._jobs.due_page(limit=self._cfg.get("stats_due_cap", 10_000), tenant=tenant)
        now = self._clock.now()
        oldest = min((float(d.get("next_run_at", now)) for d in due), default=now)
        return {"by_status": counts, "due_now": sum(1 for d in due if float(d.get("next_run_at", 0)) <= now),
                "queued_total": len(due), "dead_letters": counts.get(DEAD, 0),
                "oldest_pending_age": max(0.0, now - oldest) if due else 0.0}

    async def _emit(self, event: str, job: Job | None, extra: dict | None = None) -> None:
        if self._bus is None:
            return
        try:
            payload = {"job_id": job.id, "type": job.type, "queue": job.queue} if job else {}
            await self._bus.publish(event, {**payload, **(extra or {}), "at": self._clock.now()})
        except Exception:
            pass
