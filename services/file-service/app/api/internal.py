# =============================================================================
# file-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from pydantic import BaseModel, Field

from app.core.file_service import NotFound
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


@router.post("/files/{file_id}/signed-url", dependencies=[Depends(require_internal_token)])
async def internal_signed_url(request: Request, file_id: str,
                              tenant: str = Query(..., min_length=1)) -> dict:
    """Mint a short-lived download link for a SERVICE that has no human to borrow a token from.

    The user-facing twin takes the tenant off the caller's JWT — correctly, because a link a
    service could mint in its own name would be a link to any file on the platform. A background
    job has no JWT, so the tenant is a required PARAMETER and the file must belong to it. The
    caller was handed that tenant by whatever scheduled it; it is not choosing one.

    That check is the entire security of this endpoint. Without it the internal token — which
    every service holds — becomes a read capability over every tenant's files.

    A mismatch answers 404, identically to a file that does not exist, so this cannot be used to
    ask whether another tenant holds a given id.

    The TTL is CONFIG. A caller-supplied lifetime would let a job mint a link that outlives the
    reason it was issued, and a link is a bearer credential: whoever holds it is the reader.
    """
    svc = build_files(request)
    try:
        f = await svc.get(file_id)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if not f.tenant or f.tenant != tenant:
        # `not f.tenant` is deliberate: a file stored without a tenant belongs to nobody, and
        # answering for it here would hand it to whoever asked first.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")

    s = get_settings()
    # Audited as a SERVICE act. A download link leaving the platform is the moment the bytes
    # become reachable, and "which job handed this out" is the question asked afterwards.
    await request.app.state.auditor.record(
        "file.signed_url_issued", actor="service:internal", tenant=tenant,
        resource=f"file:{file_id}", ip=request.client.host if request.client else "")
    return {"url": f"/files/signed/{svc.signed_token(file_id)}",
            "expires_in": s.SIGNED_URL_TTL_SECONDS}


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
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = FileErasure(store=c.datastore, object_store=c.object_store, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)


class LinkSubjectsIn(BaseModel):
    tenant: str = Field(..., min_length=1)
    subjects: list[str] = Field(..., min_length=1)


@router.post("/files/{file_id}/subjects", dependencies=[Depends(require_internal_token)])
async def link_subjects(request: Request, file_id: str, body: LinkSubjectsIn) -> dict:
    """Record which people appear in a file, so erasure can reach it.

    A service that stores data on someone's behalf is the file's `owner`; the people IN it are
    named nowhere. Match footage written by a recording sink belongs to "service:stream", so a
    DSAR for a child found no files and said so — a receipt asserting absence, produced as
    evidence that a right was honoured.

    Identification is normally LATER than storage: a recording exists before anyone knows who is
    in it, and in a well-built product the naming step is also where consent is checked. So this
    is a separate call rather than an upload argument, and it is idempotent — a re-tagged face or
    a retried job must not make one file count three times in an erasure receipt.

    Only the PSEUDONYM is stored. A linkage table holding raw identifiers would be a fresh copy of
    the data erasure exists to remove.
    """
    from app.api.v1.files import build_files
    from app.core.subject_links import SubjectLinks

    c = request.app.state.container
    s = get_settings()
    files = build_files(request)
    try:
        f = await files.get(file_id)
    except NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    # Same scoping rule as every other internal read: the internal token is held by every service,
    # so without a tenant argument this would let any caller learn that a file exists — and then
    # attach a person to another tenant's data.
    if not f.tenant or f.tenant != body.tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")

    await request.app.state.auditor.record(
        "file.subjects_linked", actor="service:internal", tenant=body.tenant,
        resource=f"file:{file_id}", ip=request.client.host if request.client else "")

    links = SubjectLinks(store=c.datastore, pii_salt=s.PII_SALT)
    linked = await links.attach(tenant=body.tenant, file_id=file_id, subjects=body.subjects)
    return {"file_id": file_id, "linked": linked}
