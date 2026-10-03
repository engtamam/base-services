"""Cron scheduler — the reason job-service exists.

`tick()` (called periodically by a deployment cron / the worker loop) enqueues a job for
every schedule whose next-run has arrived, then advances next-run via the cron expression
(UTC). This is what drives every other service's sweep: billing dunning, webhook retry,
audit retention, zatca reporting, etc.

Correctness:
  • next-run is always computed in **UTC** (see core/cron.py) — DST-safe.
  • **singleton guard** — if the previous run of a schedule is still active, we skip
    enqueuing a new one (a slow nightly job must not stack ten copies of itself).
  • enqueue uses a per-fire **unique_key**, so two ticks racing on the same schedule can
    never double-enqueue the same run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.models import ACTIVE, Schedule, new_id
from app.core.stores import ScheduleStore

_log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, *, schedules: ScheduleStore, jobs, clock, cron, singleton_default: bool = True,
                 catch_up_limit: int = 100) -> None:
        self._schedules = schedules
        self._jobs = jobs                 # JobService
        self._clock = clock
        self._cron = cron
        self._singleton_default = singleton_default
        # job-008: how many MISSED occurrences a single tick will catch up per schedule. A paused/delayed
        # ticker used to drop every missed run (it advanced next_run_at from `now`, skipping the gap);
        # now it advances from each fire-time and fires the backlog, bounded so a long outage cannot
        # burst-enqueue unboundedly in one tick (the rest catch up over subsequent ticks).
        self._catch_up_limit = max(1, int(catch_up_limit))

    async def create(self, *, name: str, cron: str, queue: str = "default", tenant: str = "",
                     type: str = "", payload: dict | None = None, priority: int = 0,
                     singleton: bool | None = None) -> Schedule:
        self._cron.validate(cron)                          # raises on a bad expression
        now = self._clock.now()
        # next_after steps minute-by-minute; a legitimately-rare expression (e.g. leap-year Feb 29)
        # can iterate a long way, so run it OFF the event loop on this request path (job-003). Truly
        # impossible dom/month combos are already rejected by validate() above.
        first = await asyncio.to_thread(self._cron.next_after, cron, now)
        s = Schedule(id=new_id("sch_"), name=name, cron=cron, queue=queue, tenant=tenant,
                     type=type, payload=payload or {}, priority=priority,
                     singleton=self._singleton_default if singleton is None else singleton,
                     next_run_at=first, created=now)
        return await self._schedules.save(s)

    @staticmethod
    def _owns(s, tenant: str | None, is_operator: bool) -> bool:
        return s is not None and (is_operator or s.tenant == (tenant or "__none__"))

    async def update(self, schedule_id: str, *, tenant: str | None = None,
                     is_operator: bool = True, **kw) -> Schedule | None:
        s = await self._schedules.get(schedule_id)
        if not self._owns(s, tenant, is_operator):
            return None                                     # cross-tenant/absent: no update, no leak
        if kw.get("cron"):
            self._cron.validate(kw["cron"])
            s.next_run_at = await asyncio.to_thread(          # off the event loop (job-003)
                self._cron.next_after, kw["cron"], self._clock.now())
        for k, v in kw.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return await self._schedules.save(s)

    async def delete(self, schedule_id: str, *, tenant: str | None = None,
                     is_operator: bool = True) -> bool:
        s = await self._schedules.get(schedule_id)
        if not self._owns(s, tenant, is_operator):
            return False                                    # cross-tenant/absent: no delete, no leak
        await self._schedules.delete(schedule_id)
        return True

    async def get(self, schedule_id: str) -> Schedule | None:
        return await self._schedules.get(schedule_id)

    async def list(self, limit: int = 100, *, tenant: str | None = None,
                   is_operator: bool = False) -> list[dict]:
        return await self._schedules.list(limit, tenant=None if is_operator else tenant)

    async def tick(self) -> dict[str, Any]:
        """Fire every schedule whose time has come. Idempotent per (schedule, fire-time)."""
        now = self._clock.now()
        fired, skipped = 0, 0
        for s in await self._schedules.enabled():
            if s.next_run_at > now:
                continue
            if s.singleton and s.last_job_id:
                prev = await self._jobs.get(s.last_job_id)
                if prev is not None and prev.status in ACTIVE:
                    skipped += 1
                    s.next_run_at = self._cron.next_after(s.cron, now)   # advance, don't tight-loop
                    await self._schedules.save(s)
                    continue
            # job-008: fire EVERY missed occurrence (bounded), advancing from each fire-time rather than
            # from `now`, so a delayed ticker catches up its backlog instead of silently dropping it. Each
            # fire carries its own per-fire-time unique_key, so this is idempotent and never double-fires.
            caught = 0
            while s.next_run_at <= now and caught < self._catch_up_limit:
                fire_time = int(s.next_run_at)
                job, created = await self._jobs.enqueue(
                    queue=s.queue, tenant=s.tenant, type=s.type, payload=s.payload, priority=s.priority,
                    schedule_id=s.id, unique_key=f"sched:{s.id}:{fire_time}")
                if created:
                    fired += 1
                s.last_job_id, s.last_run_at = job.id, now
                s.next_run_at = self._cron.next_after(s.cron, s.next_run_at)   # advance from fire-time
                caught += 1
            await self._schedules.save(s)
            if s.next_run_at <= now:
                # Still behind after the per-tick cap — observable, and the next tick continues the catch-up.
                _log.warning("scheduler_behind", extra={"schedule": s.id,
                             "behind_seconds": now - s.next_run_at})
        return {"fired": fired, "skipped": skipped}
