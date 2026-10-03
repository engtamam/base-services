"""Repositories over the generic DataStore.

The claim hot path must not scan the whole job table per poll, so claimable jobs are
tracked in a small **due-index** (`jobs_due`: job_id → {next_run_at, priority, tenant,
queue}). A job is in the index exactly while it is claimable (queued & due); claiming,
finishing, or dead-lettering it removes it. The index is the only thing `claim` reads.
"""
from __future__ import annotations

from typing import Any

from app.core.models import DEAD, DispatchTarget, Job, Schedule
from app.ports.datastore import DataStore

_JOBS = "jobs"
_DUE = "jobs_due"            # job_id -> {next_run_at, priority, tenant, queue}
_LEASED = "jobs_leased"     # job_id -> {lease_expires} — reclaim reads this, never scans
_UNIQUE = "jobs_unique"      # unique_key / idempotency_key -> {job_id, ts}
_SCHEDULES = "schedules"
_DISPATCH_TARGETS = "dispatch_targets"   # queue -> {url, active} — the PUSH webhook per queue

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
    async def create(self, job: Job) -> Job:
        """Create a NEW job row via the insert-if-absent CAS (expected_version=0), NOT put(), so the
        row lands carrying version=1. This is not cosmetic: the QUEUED->LEASED claim ROTATES the row
        with a version-pinned cas_save(), and the postgres adapter's expected_version=0 branch is a
        strict INSERT ... ON CONFLICT DO NOTHING. A row written by put() has no version, so the first
        claim's cas(job.id, 0, ...) is read as insert-if-absent against an already-present row ->
        DO NOTHING -> False, and the job could NEVER be claimed in production (the memory adapter used
        to treat versionless as 0 and let it through, so every unit test passed). See identity
        RefreshStore.issue for the same fix. job.id is a fresh new_id(), so the insert always wins."""
        await self._store.cas(_JOBS, job.id, 0, job.to_dict())
        job.version = 1
        return job

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

    _STATUSES = ("queued", "leased", "done", "dead", "canceled")

    async def counts(self, *, tenant: str | None = None, cap: int = 10_000) -> dict[str, Any]:
        """How many jobs in each status. A tenant value counts ONLY that tenant's jobs via bounded,
        INDEXED per-status finds (O(that tenant's jobs), capped) — not a scan of every tenant's jobs
        (job-r2-02). tenant=None is the operator fleet aggregate: a capped whole-table scan, gated to
        platform.jobs and rate-limited at the route."""
        if tenant is not None:
            # job-009: ALWAYS populate every status with 0 so the shape is stable (a zero-count status is
            # data, not an omission), and mark truncation explicitly: a status that returns cap+1 rows is
            # reported as cap with its name listed under "_truncated", instead of a silent exact-looking
            # number that is really ">= cap".
            out: dict[str, int] = {st: 0 for st in self._STATUSES}
            truncated: list[str] = []
            for st in self._STATUSES:
                rows, _ = await self._store.find(_JOBS, {"tenant": tenant, "status": st},
                                                 limit=cap + 1, sort=("created", "desc"))
                if len(rows) > cap:
                    out[st] = cap
                    truncated.append(st)
                else:
                    out[st] = len(rows)
            if truncated:
                out["_truncated"] = truncated          # the cap is visible, not silently applied
            return out
        out = {st: 0 for st in self._STATUSES}
        # sweeper: the fleet-wide operator aggregate — a capped whole-collection count.
        for r in await self._store.scan_all(_JOBS, _INDEX_MAX):
            st = r.get("status", "?")
            out[st] = out.get(st, 0) + 1
        return out

    # ── due-index (the claim hot path) ──────────────────────────────────────────
    async def index_due(self, job: Job) -> None:
        # `due: True` is the constant filter that lets due_page() page the index with an indexed
        # WHERE + ORDER BY next_run_at (find needs a non-empty where), instead of scanning it whole.
        await self._store.put(_DUE, job.id, {"id": job.id, "due": True, "next_run_at": job.next_run_at,
                                             "priority": job.priority, "tenant": job.tenant,
                                             "queue": job.queue})

    async def unindex_due(self, job_id: str) -> None:
        await self._store.delete(_DUE, job_id)

    async def due_page(self, *, limit: int, tenant: str | None = None) -> list[dict]:
        """A BOUNDED, oldest-first page of the claimable set, served by the next_run_at index — the
        claim/stats hot path reads at most `limit` rows instead of the whole due-index (job-r2-02/03).
        Oldest-first (next_run_at asc) so a backlog drains fairly; the caller re-orders the small
        window by priority."""
        where: dict = {"due": True}
        if tenant is not None:
            where["tenant"] = tenant
        rows, _ = await self._store.find(_DUE, where, limit=max(1, int(limit)),
                                         sort=("next_run_at", "asc"))
        return rows

    async def index_leased(self, job: Job) -> None:
        # `leased: True` is the constant filter that lets leased_index() PAGE this index with an indexed
        # WHERE (find needs a non-empty where), the same shape index_due uses — so reclaim covers every
        # in-flight job instead of a single capped scan that silently drops leases past the cap (job-015).
        await self._store.put(_LEASED, job.id, {"job_id": job.id, "leased": True,
                                                "lease_expires": job.lease_expires})

    async def unindex_leased(self, job_id: str) -> None:
        await self._store.delete(_LEASED, job_id)

    async def leased_index(self) -> list[dict]:
        """In-flight jobs, for lease-expiry reclaim — indexed and PAGED to completion, so reclaim covers
        EVERY in-flight job (job-015: a single capped scan dropped leases past the cap → lost jobs)."""
        rows, cursor = [], None
        while True:
            page, cursor = await self._store.find(_LEASED, {"leased": True}, limit=1000,
                                                  after=cursor, sort=("job_id", "asc"))
            rows.extend(page)
            if not cursor:
                break
        return rows

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


class DispatchTargetStore:
    """The PUSH webhook registry — one target per queue, keyed by the queue name (the row id)."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def upsert(self, t: DispatchTarget) -> DispatchTarget:
        await self._store.put(_DISPATCH_TARGETS, t.queue, t.to_dict())
        return t

    async def get(self, queue: str) -> DispatchTarget | None:
        row = await self._store.get(_DISPATCH_TARGETS, queue)
        return DispatchTarget.from_dict(row) if row else None

    async def delete(self, queue: str) -> bool:
        if await self._store.get(_DISPATCH_TARGETS, queue) is None:
            return False
        await self._store.delete(_DISPATCH_TARGETS, queue)
        return True

    async def list(self) -> list[dict]:
        # sweeper: the target registry is global and tiny — one row per push-enabled queue, exactly
        # like the schedule catalogue. Bounded by _SCHEDULE_MAX so it can never become an unbounded read.
        return await self._store.scan_all(_DISPATCH_TARGETS, _SCHEDULE_MAX)

    async def active(self) -> list[DispatchTarget]:
        """Every queue that currently has an ACTIVE push target — what the dispatcher iterates."""
        return [DispatchTarget.from_dict(r) for r in await self.list() if r.get("active", True)]
