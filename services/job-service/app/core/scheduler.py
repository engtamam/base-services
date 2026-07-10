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

from typing import Any

from app.core.models import ACTIVE, Schedule, new_id
from app.core.stores import ScheduleStore


class Scheduler:
    def __init__(self, *, schedules: ScheduleStore, jobs, clock, cron, singleton_default: bool = True) -> None:
        self._schedules = schedules
        self._jobs = jobs                 # JobService
        self._clock = clock
        self._cron = cron
        self._singleton_default = singleton_default

    async def create(self, *, name: str, cron: str, queue: str = "default", tenant: str = "",
                     type: str = "", payload: dict | None = None, priority: int = 0,
                     singleton: bool | None = None) -> Schedule:
        self._cron.validate(cron)                          # raises on a bad expression
        now = self._clock.now()
        s = Schedule(id=new_id("sch_"), name=name, cron=cron, queue=queue, tenant=tenant,
                     type=type, payload=payload or {}, priority=priority,
                     singleton=self._singleton_default if singleton is None else singleton,
                     next_run_at=self._cron.next_after(cron, now), created=now)
        return await self._schedules.save(s)

    async def update(self, schedule_id: str, **kw) -> Schedule | None:
        s = await self._schedules.get(schedule_id)
        if s is None:
            return None
        if kw.get("cron"):
            self._cron.validate(kw["cron"])
            s.next_run_at = self._cron.next_after(kw["cron"], self._clock.now())
        for k, v in kw.items():
            if v is not None and hasattr(s, k):
                setattr(s, k, v)
        return await self._schedules.save(s)

    async def delete(self, schedule_id: str) -> None:
        await self._schedules.delete(schedule_id)

    async def get(self, schedule_id: str) -> Schedule | None:
        return await self._schedules.get(schedule_id)

    async def list(self, limit: int = 100) -> list[dict]:
        return await self._schedules.list(limit)

    async def tick(self) -> dict[str, Any]:
        """Fire every schedule whose time has come. Idempotent per (schedule, fire-time)."""
        now = self._clock.now()
        fired, skipped = 0, 0
        for s in await self._schedules.enabled():
            if s.next_run_at > now:
                continue
            fire_time = int(s.next_run_at)
            if s.singleton and s.last_job_id:
                prev = await self._jobs.get(s.last_job_id)
                if prev is not None and prev.status in ACTIVE:
                    skipped += 1
                    s.next_run_at = self._cron.next_after(s.cron, now)   # advance, don't tight-loop
                    await self._schedules.save(s)
                    continue
            job, created = await self._jobs.enqueue(
                queue=s.queue, tenant=s.tenant, type=s.type, payload=s.payload, priority=s.priority,
                schedule_id=s.id, unique_key=f"sched:{s.id}:{fire_time}")
            if created:
                fired += 1
            s.last_job_id, s.last_run_at = job.id, now
            s.next_run_at = self._cron.next_after(s.cron, now)
            await self._schedules.save(s)
        return {"fired": fired, "skipped": skipped}
