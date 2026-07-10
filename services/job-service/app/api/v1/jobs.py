# =============================================================================
# job-service — admin: schedules, jobs, DLQ/replay, cancel, monitoring
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.cron import CronError
from app.core.job_service import JobService
from app.core.scheduler import Scheduler
from app.core.stores import JobStore, ScheduleStore

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

_ADMIN = require_any_capability("platform.jobs", "jobs.manage")
_READ = require_any_capability("platform.jobs", "jobs.manage", "jobs.read")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"lease_seconds": s.LEASE_SECONDS, "claim_batch_max": s.CLAIM_BATCH_MAX,
            "heartbeat_extend": s.HEARTBEAT_EXTEND_SECONDS, "retry_base_seconds": s.RETRY_BASE_SECONDS,
            "retry_max_seconds": s.RETRY_MAX_SECONDS, "retry_max_attempts": s.RETRY_MAX_ATTEMPTS,
            "retry_jitter": s.RETRY_JITTER, "unique_ttl": s.UNIQUE_DEDUPE_TTL_SECONDS,
            "fairness": s.FAIRNESS_ENABLED, "list_limit": s.JOB_LIST_LIMIT}


def build_jobs(request: Request) -> JobService:
    c = request.app.state.container
    return JobService(jobs=JobStore(c.datastore), clock=c.clock, config=_cfg(), event_bus=c.event_bus)


def build_scheduler(request: Request) -> Scheduler:
    c = request.app.state.container
    return Scheduler(schedules=ScheduleStore(c.datastore), jobs=build_jobs(request),
                     clock=c.clock, cron=c.cron, singleton_default=get_settings().SCHEDULER_SINGLETON)


def _tenant(p: Principal, body_tenant: str | None = None) -> str:
    if "platform.jobs" in p.caps:
        return body_tenant or p.tenant or "global"
    return p.tenant or "__none__"


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron: str = Field(min_length=1, max_length=128)
    queue: str = "default"
    type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    singleton: bool | None = None
    tenant: str | None = None


# ── schedules ─────────────────────────────────────────────────────────────────
@router.post("/schedules", status_code=201, dependencies=[Depends(_ADMIN)])
async def create_schedule(request: Request, body: ScheduleIn, principal: Principal = Depends(_ADMIN)) -> dict:
    try:
        s = await build_scheduler(request).create(
            name=body.name, cron=body.cron, queue=body.queue, tenant=_tenant(principal, body.tenant),
            type=body.type, payload=body.payload, priority=body.priority, singleton=body.singleton)
    except CronError as e:
        raise HTTPException(422, f"BAD_CRON: {e}")
    return s.to_dict()


@router.get("/schedules", dependencies=[Depends(_READ)])
async def list_schedules(request: Request) -> dict:
    return {"schedules": await build_scheduler(request).list(get_settings().JOB_LIST_LIMIT)}


@router.delete("/schedules/{schedule_id}", dependencies=[Depends(_ADMIN)])
async def delete_schedule(request: Request, schedule_id: str) -> dict:
    await build_scheduler(request).delete(schedule_id)
    return {"deleted": True, "id": schedule_id}


# ── jobs / DLQ / replay / cancel ────────────────────────────────────────────────
@router.get("", dependencies=[Depends(_READ)])
async def list_jobs(request: Request, status_filter: str | None = None,
                    principal: Principal = Depends(_READ)) -> dict:
    jobs = await build_jobs(request)._jobs.list(tenant=_tenant(principal), status=status_filter,
                                                limit=get_settings().JOB_LIST_LIMIT)
    from app.core.models import Job
    return {"jobs": [Job.from_dict(r).public() for r in jobs]}


@router.get("/dead-letters", dependencies=[Depends(_READ)])
async def dead_letters(request: Request, principal: Principal = Depends(_READ)) -> dict:
    rows = await build_jobs(request)._jobs.dead_letters(_tenant(principal), get_settings().JOB_LIST_LIMIT)
    from app.core.models import Job
    return {"dead_letters": [Job.from_dict(r).public() for r in rows]}


@router.post("/{job_id}/replay", dependencies=[Depends(_ADMIN)])
async def replay(request: Request, job_id: str) -> dict:
    return await build_jobs(request).replay(job_id)


@router.post("/{job_id}/cancel", dependencies=[Depends(_ADMIN)])
async def cancel(request: Request, job_id: str) -> dict:
    return await build_jobs(request).cancel(job_id)


@router.get("/stats", dependencies=[Depends(_READ)])
async def stats(request: Request) -> dict:
    return await build_jobs(request).stats()
