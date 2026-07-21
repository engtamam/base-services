# =============================================================================
# stream-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.core.erasure import StreamErasure, _overlaps
from app.core.stores import RecordingStore
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
                                       sink=c.recording_sink)


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
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = StreamErasure(store=c.datastore, pii_salt=s.PII_SALT, sink=c.recording_sink,
                           object_store=c.object_store, max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject, source_id=body.source_id,
                              start=body.start, end=body.end)
