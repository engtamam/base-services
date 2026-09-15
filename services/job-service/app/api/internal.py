# =============================================================================
# job-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.jobs import build_dispatcher, build_jobs, build_scheduler
from app.core.dispatcher import DispatchError
from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import JobErasure
from app.manifest import build_manifest
from app.core.pii import subject_ref

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
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.post("/jobs/enqueue", status_code=201, dependencies=[Depends(require_internal_token)])
async def enqueue(request: Request, body: EnqueueIn) -> dict:
    """Any service enqueues background work. Idempotent on idempotency_key / unique_key."""
    job, created = await build_jobs(request).enqueue(**body.model_dump())
    if created:
        # Enqueued over the internal token by a peer service — actor "system". Idempotent re-enqueues
        # (created is False) are not re-recorded: the work was already witnessed.
        await request.app.state.auditor.record(
            "job.enqueued", actor="system", tenant=getattr(job, "tenant", "") or "",
            resource=f"job:{job.id}", ip=request.client.host if request.client else "",
            metadata={"type": getattr(job, "type", "")})
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


class DispatchTargetIn(BaseModel):
    queue: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    active: bool = True


@router.post("/dispatch/targets", status_code=201, dependencies=[Depends(require_internal_token)])
async def register_dispatch_target(request: Request, body: DispatchTargetIn) -> dict:
    """Register (or replace) the PUSH webhook for a queue. From now, that queue's due jobs are
    delivered by job-service POSTing each to `url`, instead of the target polling claim(). The host
    must be egress-allow-listed (SSRF). A queue with no target stays PULL — this is purely additive."""
    try:
        t = await build_dispatcher(request).register(
            queue=body.queue, url=body.url, active=body.active)
    except DispatchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await request.app.state.auditor.record(
        "dispatch.target_registered", actor="system", tenant="",
        resource=f"queue:{body.queue}", ip=request.client.host if request.client else "",
        metadata={"active": str(body.active)})
    return t.to_dict()


@router.get("/dispatch/targets", dependencies=[Depends(require_internal_token)])
async def list_dispatch_targets(request: Request) -> dict:
    return {"targets": await build_dispatcher(request).list_targets()}


@router.delete("/dispatch/targets/{queue}", dependencies=[Depends(require_internal_token)])
async def delete_dispatch_target(request: Request, queue: str) -> dict:
    """Unregister a queue's push webhook — it reverts to PULL immediately."""
    removed = await build_dispatcher(request).unregister(queue)
    if removed:
        await request.app.state.auditor.record(
            "dispatch.target_removed", actor="system", tenant="",
            resource=f"queue:{queue}", ip=request.client.host if request.client else "")
    return {"removed": removed}


@router.post("/dispatch/run", dependencies=[Depends(require_internal_token)])
async def dispatch_run(request: Request) -> dict:
    """Deliver one batch of due jobs on every push-enabled queue. Driven by the SAME central ticker
    that calls scheduler/tick and jobs/reclaim — so no consuming service needs its own CronJob. A
    no-op when push is disabled."""
    return await build_dispatcher(request).dispatch_batch()


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
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = JobErasure(store=c.datastore, pii_salt=s.PII_SALT, text_keys=s.erasure_text_keys,
                  max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What this service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext.
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) "
            "on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


class BackupExportIn(BaseModel):
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    into_tenant: str = ""
    # AUTHORITATIVE undo: also delete rows present now but absent from the archive
    # (backup-r2-01). Set only by the undo path; never combined with into_tenant.
    prune: bool = False


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of this service's declared data."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing."""
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP, prune=body.prune,
            scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
