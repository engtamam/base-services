"""Repositories over the generic DataStore.

The claim hot path must not scan the whole job table per poll, so claimable jobs are
tracked in a small **due-index** (`jobs_due`: job_id → {next_run_at, priority, tenant,
queue}). A job is in the index exactly while it is claimable (queued & due); claiming,
finishing, or dead-lettering it removes it. The index is the only thing `claim` reads.
"""
from __future__ import annotations

from typing import Any

from app.core.models import DEAD, Job, Schedule
from app.ports.datastore import DataStore

_JOBS = "jobs"
_DUE = "jobs_due"            # job_id -> {next_run_at, priority, tenant, queue}
_LEASED = "jobs_leased"     # job_id -> {lease_expires} — reclaim reads this, never scans
_UNIQUE = "jobs_unique"      # unique_key / idempotency_key -> {job_id, ts}
_SCHEDULES = "schedules"
_SCAN = 1_000_000


class JobStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    # ── jobs ──────────────────────────────────────────────────────────────────
    async def save(self, job: Job) -> Job:
        await self._store.put(_JOBS, job.id, job.to_dict())
        return job

    async def get(self, job_id: str) -> Job | None:
        row = await self._store.get(_JOBS, job_id)
        return Job.from_dict(row) if row else None

    async def list(self, *, tenant: str | None = None, queue: str | None = None,
                   status: str | None = None, limit: int = 100) -> list[dict]:
        rows = await self._store.list(_JOBS, _SCAN)
        if tenant is not None:
            rows = [r for r in rows if r.get("tenant") == tenant]
        if queue is not None:
            rows = [r for r in rows if r.get("queue") == queue]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created", 0), reverse=True)
        return rows[:limit]

    async def dead_letters(self, tenant: str | None = None, limit: int = 100) -> list[dict]:
        return await self.list(tenant=tenant, status=DEAD, limit=limit)

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in await self._store.list(_JOBS, _SCAN):
            out[r.get("status", "?")] = out.get(r.get("status", "?"), 0) + 1
        return out

    # ── due-index (the claim hot path) ──────────────────────────────────────────
    async def index_due(self, job: Job) -> None:
        await self._store.put(_DUE, job.id, {"id": job.id, "next_run_at": job.next_run_at,
                                             "priority": job.priority, "tenant": job.tenant,
                                             "queue": job.queue})

    async def unindex_due(self, job_id: str) -> None:
        await self._store.delete(_DUE, job_id)

    async def due_index(self) -> list[dict]:
        """The claimable set. In a KV store this lists the SMALL index, not the job table;
        a SQL adapter would serve this from an index on (queue, next_run_at, priority)."""
        return await self._store.list(_DUE, _SCAN)

    async def index_leased(self, job: Job) -> None:
        await self._store.put(_LEASED, job.id, {"job_id": job.id, "lease_expires": job.lease_expires})

    async def unindex_leased(self, job_id: str) -> None:
        await self._store.delete(_LEASED, job_id)

    async def leased_index(self) -> list[dict]:
        """In-flight jobs, for lease-expiry reclaim — indexed, never a job-table scan."""
        return await self._store.list(_LEASED, _SCAN)

    # ── idempotency / uniqueness ────────────────────────────────────────────────
    async def unique_get(self, key: str, *, now: float, ttl: int) -> str | None:
        row = await self._store.get(_UNIQUE, key)
        if not row:
            return None
        if ttl and (now - float(row.get("ts", 0) or 0)) > ttl:
            return None
        return row.get("job_id")

    async def unique_put(self, key: str, job_id: str, now: float) -> None:
        await self._store.put(_UNIQUE, key, {"job_id": job_id, "ts": now})

    async def unique_clear(self, key: str) -> None:
        if key:
            await self._store.delete(_UNIQUE, key)


class ScheduleStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, s: Schedule) -> Schedule:
        await self._store.put(_SCHEDULES, s.id, s.to_dict())
        return s

    async def get(self, schedule_id: str) -> Schedule | None:
        row = await self._store.get(_SCHEDULES, schedule_id)
        return Schedule.from_dict(row) if row else None

    async def delete(self, schedule_id: str) -> None:
        await self._store.delete(_SCHEDULES, schedule_id)

    async def list(self, limit: int = 100) -> list[dict]:
        return (await self._store.list(_SCHEDULES, _SCAN))[:limit]

    async def enabled(self) -> list[Schedule]:
        return [Schedule.from_dict(r) for r in await self._store.list(_SCHEDULES, _SCAN)
                if r.get("enabled")]
