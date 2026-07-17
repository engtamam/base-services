# =============================================================================
# file-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.core.erasure import FileErasure
from app.core.scan_worker import ScanWorker
from app.core.upload_session import UploadSessionStore
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])

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


@router.post("/files/scan", dependencies=[Depends(require_internal_token)])
async def drain_scan_queue(request: Request) -> dict:
    """Hash + scan every staged large upload waiting in the queue, then finalize each session.

    Driven by a CronJob (the platform pattern for background sweeps). Idempotent: a session already
    finalized is skipped, so a re-run never double-processes.
    """
    from app.api.v1.files import build_files   # local import avoids a route<->route import cycle
    c = request.app.state.container
    s = get_settings()
    files = build_files(request)
    worker = ScanWorker(files=files, sessions=UploadSessionStore(c.datastore),
                        object_store=c.object_store, scanner=c.scanner, queue=c.queue,
                        scan_queue=s.FILE_SCAN_QUEUE, scan_policy=s.LARGE_UPLOAD_SCAN)
    processed = await worker.run_once()
    return {"processed": processed}


@router.post("/files/sweep-uploads", dependencies=[Depends(require_internal_token)])
async def sweep_uploads(request: Request) -> dict:
    """Abort large-upload sessions older than UPLOAD_SESSION_TTL_SECONDS — releases their multiparts.

    Driven by a CronJob. An initiated-but-never-completed upload leaves parts costing storage; this
    is what stops them accruing forever.
    """
    import datetime as _dt

    from app.api.v1.files import build_files
    s = get_settings()
    files = build_files(request)
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(seconds=s.UPLOAD_SESSION_TTL_SECONDS)).isoformat()
    return await files.sweep_stale_sessions(cutoff)


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    c = request.app.state.container
    s = get_settings()
    eraser = FileErasure(store=c.datastore, object_store=c.object_store, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
