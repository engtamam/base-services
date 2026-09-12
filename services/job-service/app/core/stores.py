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

# The indexes below hold one row per CLAIMABLE / IN-FLIGHT job, not one per job ever enqueued.
# That is the whole reason they exist, so reading them whole is the operation. The bound is a
# blast radius, not a page: if a deployment ever has 50 000 simultaneously-due jobs, the sweeper
# should be visibly truncating rather than quietly reading a million rows.
_INDEX_MAX = 50_000
_SCHEDULE_MAX = 10_000


class JobStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    # ── jobs ──────────────────────────────────────────────────────────────────
    async def save(self, job: Job) -> Job:
        await self._store.put(_JOBS, job.id, job.to_dict())
        return job

    async def cas_save(self, job: Job, expected_version: int) -> bool:
        """Version-checked write for the atomic claim: True if this write won, False if another worker
        changed the job first. The QUEUED->LEASED transition must go through this so two workers cannot
        both lease (double-execute) the same job."""
        return await self._store.cas(_JOBS, job.id, expected_version, job.to_dict())

    async def get(self, job_id: str) -> Job | None:
        row = await self._store.get(_JOBS, job_id)
        return Job.from_dict(row) if row else None

    async def list(self, *, tenant: str | None = None, queue: str | None = None,
                   status: str | None = None, limit: int = 100,
                   after: str | None = None) -> list[dict]:
        """Filtered job list. Whatever the caller filtered by is pushed into the datastore.

        Every filter used to be applied in Python after reading the whole job table. A tenant
        asking for its own five jobs paid for everyone's history, and past the cap it could be
        shown none of its own.

        An unfiltered call is an operator listing the queue. It is bounded and marked as such.
        """
        where = {k: v for k, v in (("tenant", tenant), ("queue", queue), ("status", status))
                 if v is not None}
        if not where:
            # sweeper: an operator's unfiltered queue view. There is no tenant to scope by.
            rows = await self._store.scan_all(_JOBS, _INDEX_MAX)
            rows.sort(key=lambda r: r.get("created", 0), reverse=True)
            return rows[:limit]
        rows, _ = await self._store.find(_JOBS, where, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows

    async def dead_letters(self, tenant: str | None = None, limit: int = 100) -> list[dict]:
        return await self.list(tenant=tenant, status=DEAD, limit=limit)

    async def counts(self, *, tenant: str | None = None) -> dict[str, int]:
        """How many jobs in each status. tenant=None counts across all tenants (operator view); a
        tenant value counts only that tenant's jobs."""
        out: dict[str, int] = {}
        # sweeper: counting every job IS the operation. Not on any request path - the
        # metrics endpoint calls it on a timer.
        for r in await self._store.scan_all(_JOBS, _INDEX_MAX):
            if tenant is not None and r.get("tenant", "") != tenant:
                continue
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
        # sweeper: `jobs_due` holds one row per CLAIMABLE job and is emptied as jobs are
        # claimed. Reading it whole is how the worker finds work; it is not the job table.
        return await self._store.scan_all(_DUE, _INDEX_MAX)

    async def index_leased(self, job: Job) -> None:
        await self._store.put(_LEASED, job.id, {"job_id": job.id, "lease_expires": job.lease_expires})

    async def unindex_leased(self, job_id: str) -> None:
        await self._store.delete(_LEASED, job_id)

    async def leased_index(self) -> list[dict]:
        """In-flight jobs, for lease-expiry reclaim — indexed, never a job-table scan."""
        # sweeper: `jobs_leased` holds one row per IN-FLIGHT job. The reclaimer's whole job is
        # to look at all of them and expire the dead leases.
        return await self._store.scan_all(_LEASED, _INDEX_MAX)

    # ── idempotency / uniqueness ────────────────────────────────────────────────
    async def unique_get(self, key: str, *, now: float, ttl: int) -> str | None:
        row = await self._store.get(_UNIQUE, key)
        if not row:
            return None
        if ttl and (now - float(row.get("ts", 0) or 0)) > ttl:
            return None
        return row.get("job_id")

    async def unique_reserve(self, key: str, job_id: str, now: float, *, ttl: int = 0) -> bool:
        """Atomically claim a unique/idempotency key (cas insert-if-absent). True if WE reserved it,
        False if it is already held. Replaces the racy unique_get->unique_put pair so two concurrent
        same-key enqueues cannot both create a job. An expired reservation (past ttl) is re-claimable."""
        if await self._store.cas(_UNIQUE, key, 0, {"job_id": job_id, "ts": now}):
            return True
        if ttl:
            row = await self._store.get(_UNIQUE, key)
            if row and (now - float(row.get("ts", 0) or 0)) > ttl:
                await self._store.put(_UNIQUE, key, {"job_id": job_id, "ts": now})
                return True
        return False

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

    async def list(self, limit: int = 100, *, tenant: str | None = None) -> list[dict]:
        # sweeper: the schedule catalogue is global and small - one row per cron entry. A tenant
        # caller sees ONLY its own schedules (cross-tenant IDOR otherwise); tenant=None is the
        # platform-operator view of every tenant.
        rows = await self._store.scan_all(_SCHEDULES, _SCHEDULE_MAX)
        if tenant is not None:
            rows = [r for r in rows if r.get("tenant", "") == tenant]
        return rows[:limit]

    async def enabled(self) -> list[Schedule]:
        """The schedules the ticker must consider. Runs on a timer, not on a request."""
        rows, cursor = [], None
        while True:
            page, cursor = await self._store.find(_SCHEDULES, {"enabled": True},
                                                  limit=500, after=cursor, sort=("id", "asc"))
            rows.extend(page)
            if not cursor:
                break
        return [Schedule.from_dict(r) for r in rows]
