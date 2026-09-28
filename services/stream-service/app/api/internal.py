# =============================================================================
# stream-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.erasure import StreamErasure, _overlaps
from app.core.stores import RecordingStore
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


@router.post("/stream/sweep", dependencies=[Depends(require_internal_token)])
async def sweep_sessions(request: Request) -> dict:
    """Enforce guards and reconcile placement across all live sessions.

    Driven by a CronJob (the platform pattern). THIS is what makes 'a session can never run forever'
    operationally true: a session past its max_duration/idle/lease is stopped here, and a session
    whose engine process died is reaped. Without a caller, the guards are only advisory.
    """
    from app.api.v1.stream import build_stream_service
    from app.core.stores import TriggerStore
    c = request.app.state.container
    s = get_settings()
    svc = build_stream_service(request)
    shipped = await svc.ship_live_recordings()   # move completed segments off the node first
    motion = await svc.poll_motion(c.motion, TriggerStore(c.datastore),
                                   debounce_s=s.MOTION_DEBOUNCE_SECONDS)
    result = await svc.enforce_and_reconcile()
    result.update(shipped)
    result.update(motion)
    return result


class HeartbeatIn(BaseModel):
    stats: dict = {}


@router.post("/stream/sessions/{session_id}/heartbeat", dependencies=[Depends(require_internal_token)])
async def heartbeat(request: Request, session_id: str, body: HeartbeatIn) -> dict:
    """Engine → service: extend the session lease and record stats (fps/bitrate/bytes). The size
    guard is checked here, so a recording that hit its cap stops immediately."""
    from app.api.v1.stream import build_stream_service
    sess = await build_stream_service(request).heartbeat(session_id, stats=body.stats or None)
    return sess.public_dict() if sess is not None else {"status": "gone"}


@router.post("/stream/exports/run", dependencies=[Depends(require_internal_token)])
async def run_exports(request: Request) -> dict:
    """Produce queued clips. Called by a CronJob or a worker Deployment.

    Safe to run from several replicas at once: each dequeues a different job, and an export that
    is no longer PENDING is skipped rather than redone. Capped per run so a backlog degrades into
    several runs instead of one that never finishes.
    """
    from app.api.v1.stream import _exports
    s = get_settings()
    return await _exports(request).drain(s.EXPORT_DRAIN_MAX)


@router.post("/stream/retention", dependencies=[Depends(require_internal_token)])
async def enforce_retention(request: Request) -> dict:
    """Delete recordings past RETENTION_DAYS (legal-held ones are exempt). CronJob-driven."""
    from app.api.v1.stream import build_stream_service
    from app.core.stores import RecordingStore
    c = request.app.state.container
    s = get_settings()
    svc = build_stream_service(request)
    return await svc.enforce_retention(days=s.RETENTION_DAYS,
                                       recordings=RecordingStore(c.datastore),
                                       sink=c.recording_sink,
                                       max_bytes=s.RETENTION_MAX_BYTES)


class EraseIn(BaseModel):
    tenant: str
    subject: str
    # Video is keyed by camera and time, not by person. These narrow the request to footage the
    # caller has IDENTIFIED; without source_id nothing is deleted, because guessing which
    # recordings hold a person would delete other people's footage.
    source_id: str = ""
    start: str = ""
    end: str = ""


@router.get("/stream/segments", dependencies=[Depends(require_internal_token)])
async def list_segments(
    request: Request,
    tenant: str = Query(..., min_length=1),
    source_id: str = Query(..., min_length=1),
    start: str = Query(""),
    end: str = Query(""),
) -> dict:
    """The segment file_ids for footage identified by camera + window.

    Stream stores footage by CAMERA and TIME, never by person — it cannot know who is in a frame
    without running facial recognition, which would create more biometric data than it answers.
    So stream's own erasure needs the caller to identify footage by source_id + window (see
    /privacy/erase), and that is the primary path: it deletes the footage outright.

    This is the OTHER path, for the service that DOES have identity (squad, which records who a
    coach named). Given the same camera + window, it returns each segment's `file_id` in
    file-service so the caller can attach the subject there via POST /internal/files/{id}/subjects
    — after which a file-service DSAR for that person reaches the footage through the subject-link.
    Either path erases the video; neither leaves it stranded.

    `source_id` is REQUIRED for the same reason erase requires it: returning "every segment for a
    tenant" would hand back other people's footage identifiers on an unscoped call. Locked
    (legal-hold) recordings are still listed — a hold governs deletion, not who may be linked.
    """
    c = request.app.state.container
    s = get_settings()
    recordings = RecordingStore(c.datastore)

    out: list[dict] = []
    scanned = 0
    after: str | None = None
    while scanned < s.ERASURE_MAX_SCAN:
        recs, after = await recordings.list_for_tenant(tenant, source_id=source_id,
                                                       limit=_SEG_PAGE, after=after)
        if not recs:
            break
        for rec in recs:
            scanned += 1
            d = rec.to_dict()
            if not _overlaps(d, start, end):
                continue
            for seg in rec.segments or []:
                ref = seg.get("ref")
                if not ref:
                    continue
                out.append({"recording_id": rec.id, "file_id": ref,
                            "start": seg.get("start", ""), "end": seg.get("end", ""),
                            "locked": bool(rec.locked)})
        if not after:
            break

    return {"tenant": tenant, "source_id": source_id, "segments": out,
            "truncated": scanned >= s.ERASURE_MAX_SCAN}


_SEG_PAGE = 500


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase footage identified by camera + window. Locked (legal-hold) recordings are retained.

    With no source_id this answers `identification_not_possible` rather than "nothing here" — the
    person may well be on camera, and claiming otherwise would be a lie.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = StreamErasure(store=c.datastore, pii_salt=s.PII_SALT, sink=c.recording_sink,
                           object_store=c.object_store, max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject, source_id=body.source_id,
                              start=body.start, end=body.end)


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
