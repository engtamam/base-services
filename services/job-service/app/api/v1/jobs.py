# =============================================================================
# job-service — admin: schedules, jobs, DLQ/replay, cancel, monitoring
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.cron import CronError
from app.core.dispatcher import Dispatcher
from app.core.job_service import JobService
from app.core.scheduler import Scheduler
from app.core.stores import DispatchTargetStore, JobStore, ScheduleStore

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
    return JobService(jobs=JobStore(c.datastore), clock=c.clock, config=_cfg(), event_bus=c.event_bus,
                      telemetry=getattr(c, "telemetry", None))


def build_scheduler(request: Request) -> Scheduler:
    c = request.app.state.container
    s = get_settings()
    return Scheduler(schedules=ScheduleStore(c.datastore), jobs=build_jobs(request),
                     clock=c.clock, cron=c.cron, singleton_default=s.SCHEDULER_SINGLETON,
                     catch_up_limit=s.SCHEDULER_CATCH_UP_LIMIT)


def _dispatch_cfg() -> dict[str, Any]:
    s = get_settings()
    return {"enabled": s.PUSH_DISPATCH_ENABLED, "batch": s.PUSH_DISPATCH_BATCH,
            "timeout": s.PUSH_DISPATCH_TIMEOUT_SECONDS,
            "allowed_hosts": s.egress_allowed_hosts,
            # Sign with the dedicated push secret when set, else the shared internal token so a push is
            # signed out of the box (the receiver holds the same token to verify).
            "signing_secret": s.PUSH_SIGNING_SECRET or s.INTERNAL_SERVICE_TOKEN,
            "concurrency": s.PUSH_DISPATCH_CONCURRENCY}
    # job-013: PUSH_SIGNATURE_MAX_SKEW_SECONDS is a RECEIVER concern (its verify() rejects a stale/early
    # signature) — the sender never reads it. It is intentionally NOT in the dispatcher config; the
    # setting remains for receivers that verify this service's pushes.


def build_dispatcher(request: Request) -> Dispatcher:
    c = request.app.state.container
    return Dispatcher(targets=DispatchTargetStore(c.datastore), jobs=build_jobs(request),
                      clock=c.clock, config=_dispatch_cfg())


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
    await request.app.state.auditor.record(
        "schedule.created", actor=principal.subject, tenant=_tenant(principal, body.tenant),
        resource=f"schedule:{s.id}", ip=request.client.host if request.client else "")
    return s.to_dict()


@router.get("/schedules", dependencies=[Depends(_READ)])
async def list_schedules(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"schedules": await build_scheduler(request).list(
        get_settings().JOB_LIST_LIMIT, tenant=_tenant(principal),
        is_operator="platform.jobs" in principal.caps)}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str,
                          principal: Principal = Depends(_ADMIN)) -> dict:
    deleted = await build_scheduler(request).delete(
        schedule_id, tenant=principal.tenant, is_operator="platform.jobs" in principal.caps)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "schedule.deleted", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"schedule:{schedule_id}", ip=request.client.host if request.client else "")
    return {"deleted": True, "id": schedule_id}


# ── jobs / DLQ / replay / cancel ────────────────────────────────────────────────
@router.get("", dependencies=[Depends(_READ)])
async def list_jobs(request: Request, status_filter: str | None = None,
                    principal: Principal = Depends(_READ)) -> dict:
    # A platform operator sees fleet-wide jobs; a tenant reader only its own (job-002). Was scoped to
    # the literal tenant "global" via _tenant(), so an operator's list silently showed nothing.
    tenant = None if "platform.jobs" in principal.caps else _tenant(principal)
    jobs = await build_jobs(request)._jobs.list(tenant=tenant, status=status_filter,
                                                limit=get_settings().JOB_LIST_LIMIT)
    from app.core.models import Job
    return {"jobs": [Job.from_dict(r).public() for r in jobs]}


@router.get("/dead-letters", dependencies=[Depends(_READ)])
async def dead_letters(request: Request, principal: Principal = Depends(_READ)) -> dict:
    # Fleet-wide for a platform operator; own-tenant for a tenant reader (job-002).
    tenant = None if "platform.jobs" in principal.caps else _tenant(principal)
    rows = await build_jobs(request)._jobs.dead_letters(tenant, get_settings().JOB_LIST_LIMIT)
    from app.core.models import Job
    return {"dead_letters": [Job.from_dict(r).public() for r in rows]}


@router.post("/{job_id}/replay")
async def replay(request: Request, job_id: str,
                 principal: Principal = Depends(_ADMIN)) -> dict:
    # Record only a replay that ACTUALLY happened: replay() returns {ok:false} (not_found/not_dead)
    # without mutating, so a write-ahead audit here logged an action that never occurred and let a
    # caller probe arbitrary job ids. Record after a successful outcome.
    result = await build_jobs(request).replay(
        job_id, tenant=principal.tenant, is_operator="platform.jobs" in principal.caps)
    if result.get("ok"):
        # Audit the AFFECTED job's tenant, not the operator's own (job-016): a cross-tenant operator
        # replay otherwise files the row under the wrong tenant.
        await request.app.state.auditor.record(
            "job.replayed", actor=principal.subject,
            tenant=result.get("tenant") or principal.tenant or "", resource=f"job:{job_id}",
            ip=request.client.host if request.client else "")
    return result


@router.post("/{job_id}/cancel")
async def cancel(request: Request, job_id: str,
                 principal: Principal = Depends(_ADMIN)) -> dict:
    # Record only a cancel that ACTUALLY happened (see replay): cancel() returns {ok:false} without
    # mutating for a not_found/not_cancelable job.
    result = await build_jobs(request).cancel(
        job_id, tenant=principal.tenant, is_operator="platform.jobs" in principal.caps)
    if result.get("ok"):
        await request.app.state.auditor.record(
            "job.cancelled", actor=principal.subject,
            tenant=result.get("tenant") or principal.tenant or "", resource=f"job:{job_id}",
            ip=request.client.host if request.client else "")
    return result


@router.get("/stats", dependencies=[Depends(_READ), Depends(rate_limit)])
async def stats(request: Request, principal: Principal = Depends(_READ)) -> dict:
    # job-011: the fleet-wide operator path runs a capped whole-table count, so the route IS rate-limited
    # (the previous "rate-limited at the route" comment was not backed by any dependency). Configure a
    # dedicated RATELIMIT_ROUTES class for /api/v1/jobs/stats to tighten it below the default if needed.
    # a platform operator sees fleet-wide stats; a tenant reader sees only its own.
    tenant = None if "platform.jobs" in principal.caps else _tenant(principal)
    return await build_jobs(request).stats(tenant=tenant)


# Registered LAST so the literal GET routes (/dead-letters, /stats) are matched before this
# parameterised one — otherwise `/stats` would bind job_id="stats".
@router.get("/{job_id}", dependencies=[Depends(_READ)])
async def get_job(request: Request, job_id: str, principal: Principal = Depends(_READ)) -> dict:
    """One job's full detail — status, attempts, last error, payload — the see-before-you-act read
    behind replay and cancel: an operator decides whether to replay from WHY it failed. A platform
    operator sees any job; a tenant reader is scoped to its own, and a job it may not see reads as
    absent (404) rather than revealing that it exists."""
    job = await build_jobs(request).get(job_id)
    if job is None or (
            "platform.jobs" not in principal.caps and job.tenant != (principal.tenant or "__none__")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return job.public()
