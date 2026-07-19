# =============================================================================
# job-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.jobs import build_jobs, build_scheduler
from app.config import Settings, get_settings
from app.core.erasure import JobErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class EnqueueIn(BaseModel):
    queue: str = "default"
    tenant: str = ""
    type: str = Field(default="", max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    delay_seconds: float = 0
    max_attempts: int | None = None
    unique_key: str = Field(default="", max_length=256)
    idempotency_key: str = Field(default="", max_length=256)


class ClaimIn(BaseModel):
    worker: str = Field(min_length=1, max_length=128)
    queue: str | None = None
    batch: int = Field(default=1, ge=1)


class AckIn(BaseModel):
    job_id: str
    lease_token: str
    result: dict[str, Any] = Field(default_factory=dict)


class NackIn(BaseModel):
    job_id: str
    lease_token: str
    error: str = ""


class LeaseIn(BaseModel):
    job_id: str
    lease_token: str

_HEADER = "X-Internal-Token"


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Deny unless the caller presents the exact internal token (timing-safe)."""
    expected = settings.INTERNAL_SERVICE_TOKEN
    provided = x_internal_token or ""
    # hmac.compare_digest is constant-time and length-safe.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED"
        )


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.post("/jobs/enqueue", status_code=201, dependencies=[Depends(require_internal_token)])
async def enqueue(request: Request, body: EnqueueIn) -> dict:
    """Any service enqueues background work. Idempotent on idempotency_key / unique_key."""
    job, created = await build_jobs(request).enqueue(**body.model_dump())
    return {"job_id": job.id, "status": job.status, "created": created}


@router.post("/jobs/claim", dependencies=[Depends(require_internal_token)])
async def claim(request: Request, body: ClaimIn) -> dict:
    """A worker leases up to `batch` due jobs (atomic; each carries a lease_token)."""
    return {"jobs": await build_jobs(request).claim(worker=body.worker, queue=body.queue, batch=body.batch)}


@router.post("/jobs/ack", dependencies=[Depends(require_internal_token)])
async def ack(request: Request, body: AckIn) -> dict:
    return await build_jobs(request).ack(body.job_id, body.lease_token, body.result)


@router.post("/jobs/nack", dependencies=[Depends(require_internal_token)])
async def nack(request: Request, body: NackIn) -> dict:
    return await build_jobs(request).nack(body.job_id, body.lease_token, body.error)


@router.post("/jobs/heartbeat", dependencies=[Depends(require_internal_token)])
async def heartbeat(request: Request, body: LeaseIn) -> dict:
    return await build_jobs(request).heartbeat(body.job_id, body.lease_token)


@router.post("/jobs/reclaim", dependencies=[Depends(require_internal_token)])
async def reclaim(request: Request) -> dict:
    """Return crashed workers' expired-lease jobs to the queue (visibility timeout)."""
    return await build_jobs(request).reclaim()


@router.post("/scheduler/tick", dependencies=[Depends(require_internal_token)])
async def tick(request: Request) -> dict:
    """Fire due cron schedules. A deployment cron / the worker loop calls this."""
    return await build_scheduler(request).tick()


@router.get("/manifest", dependencies=[Depends(require_internal_token)])
async def internal_manifest(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    container = getattr(request.app.state, "container", None)
    ports = container.describe() if container is not None else []
    manifest = build_manifest(settings, ports=ports)
    guard = getattr(request.app.state, "cwe_guard", None)
    if guard is not None:
        manifest["security"] = {"cwe_guard": guard.report()}
    return manifest


class EraseIn(BaseModel):
    tenant: str
    subject: str


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Remove the data subject from this service's untyped payloads."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = JobErasure(store=c.datastore, pii_salt=s.PII_SALT, text_keys=s.erasure_text_keys,
                  max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)
