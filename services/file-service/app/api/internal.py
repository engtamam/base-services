# =============================================================================
# file-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from pydantic import BaseModel, Field

from app.core.file_service import NotFound
from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import FileErasure
from app.core.scan_worker import ScanWorker
from app.core.upload_session import UploadSessionStore
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])

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
    svc = await build_files(request)
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
    files = await build_files(request)
    worker = ScanWorker(files=files, sessions=UploadSessionStore(c.datastore),
                        object_store=c.object_store, scanner=c.scanner, queue=c.queue,
                        scan_queue=s.FILE_SCAN_QUEUE, scan_policy=s.LARGE_UPLOAD_SCAN,
                        lease_seconds=s.FILE_SCAN_LEASE_SECONDS,
                        auditor=getattr(request.app.state, "auditor", None))
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
    files = await build_files(request)
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
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
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
    files = await build_files(request)
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


async def _rebuild_blob_refs(datastore, scan_cap: int) -> int:
    """Rebuild the per-blob reference count from the restored files — it is DERIVED, not archived.

    A blob's ref-count is exactly the number of files that point at its sha256 (incr_ref runs once
    per file, decr_ref once per delete). Recomputing it from `files` keeps a restored blob from
    carrying a zero count that a later delete would read as "unreferenced" and wrongly GC. Idempotent
    — it recomputes the same counts — and it writes the same {"count": n} row the incr adapter uses.
    """
    counts: dict[str, int] = {}
    for row in await datastore.scan_all("files", scan_cap):
        sha = row.get("sha256") or row.get("storage_key")
        if sha:
            counts[sha] = counts.get(sha, 0) + 1
    for sha, n in counts.items():
        await datastore.put("blob_refs", sha, {"count": n})
    return len(counts)


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    After the file records are restored, the per-blob reference counts are rebuilt from them — they
    are derived, not archived, so a restore that skipped them would leave every blob at count 0.
    """
    datastore = request.app.state.container.datastore
    _, unseal = _sealer(settings)
    try:
        result = await _backup.import_archive(
            datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    result["blob_refs_rebuilt"] = await _rebuild_blob_refs(
        datastore, settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    return result
