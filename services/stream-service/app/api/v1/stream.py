# =============================================================================
# stream-service — user tier: sources, sessions, playback
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_tenant
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.sealing import redact_url, source_aad
from app.core.ssrf import SsrfBlocked
from app.core.recording import RecordingShipper
from app.core.exporting import ExportService
from app.core.stores import (ExportStore, RecordingStore, SessionStore, SourceStore,
                              TriggerStore)
from app.core.stream_service import (
    QuotaExceeded, SourceNotFound, StreamConfig, StreamService,
)

_REC_EXT = {"fmp4": "mp4", "mp4": "mp4", "mkv": "mkv"}

router = APIRouter(prefix="/api/v1", tags=["stream"])

_MANAGE = require_any_capability("platform.stream", "stream.manage")
_READ = require_any_capability("platform.stream", "stream.manage", "stream.view")


def build_stream_service(request: Request) -> StreamService:
    c = request.app.state.container
    s = get_settings()
    cfg = StreamConfig(
        default_mode=s.SESSION_DEFAULT_MODE, default_outputs=s.default_outputs,
        max_duration_s=s.SESSION_MAX_DURATION_SECONDS, idle_timeout_s=s.SESSION_IDLE_TIMEOUT_SECONDS,
        max_size_bytes=s.SESSION_MAX_SIZE_BYTES, lease_seconds=s.SESSION_LEASE_SECONDS,
        viewer_grace_s=s.VIEWER_GRACE_SECONDS, one_session_per_source=s.ONE_SESSION_PER_SOURCE,
        max_concurrent_global=s.STREAM_MAX_CONCURRENT_SESSIONS_GLOBAL,
        max_concurrent_per_tenant=s.STREAM_MAX_CONCURRENT_SESSIONS_PER_TENANT,
        allowed_hosts=s.stream_allowed_hosts, allow_private=s.STREAM_ALLOW_PRIVATE,
        playback_ttl=s.PLAYBACK_URL_TTL_SECONDS, output_root=s.OBJECTSTORE_ROOT + "/hls",
        webrtc_enabled=s.WEBRTC_ENABLED, webrtc_media_base=s.WEBRTC_MEDIA_BASE_URL,
        hls_segment_seconds=s.HLS_SEGMENT_SECONDS, hls_window_segments=s.HLS_WINDOW_SEGMENTS,
        record_segment_seconds=s.RECORD_SEGMENT_SECONDS, record_format=s.RECORD_FORMAT,
        connect_timeout_s=s.SESSION_START_TIMEOUT_SECONDS, reconnect_max=s.RECONNECT_MAX,
        reconnect_backoff_ms=s.RECONNECT_BACKOFF_MS,
        viewer_mark_debounce_s=s.VIEWER_MARK_DEBOUNCE_SECONDS)
    shipper = RecordingShipper(sink=c.recording_sink, recordings=RecordingStore(c.datastore),
                               record_segment_seconds=s.RECORD_SEGMENT_SECONDS)
    return StreamService(
        sources=SourceStore(c.datastore), sessions=SessionStore(c.datastore),
        placer=c.session_placer, engine=c.stream_engine, sealer=c.sealer,
        signed_playback=c.signed_playback, cfg=cfg, events=c.event_bus,
        shipper=shipper, record_ext=_REC_EXT.get(s.RECORD_FORMAT, "mp4"),
        media_egress=c.media_egress)


# ── request bodies ────────────────────────────────────────────────────────────
class SourceIn(BaseModel):
    name: str = Field(max_length=200)
    url: str = Field(max_length=2000)
    transport: str = Field(default="tcp")
    kind: str = Field(default="rtsp")
    onvif: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    group_id: str = Field(default="")
    default_profile: str = Field(default="default")


class Guards(BaseModel):
    max_duration_s: int | None = None
    idle_timeout_s: int | None = None
    max_size_bytes: int | None = None
    schedule: dict | None = None


class SessionIn(BaseModel):
    source_id: str
    mode: str | None = None
    outputs: list[str] | None = None
    profile: str | None = None
    guards: Guards | None = None


# ── sources ────────────────────────────────────────────────────────────────────
@router.post("/sources", status_code=201, dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def register_source(request: Request, body: SourceIn,
                          p: Principal = Depends(require_tenant)) -> dict:
    svc = build_stream_service(request)
    try:
        src = await svc.register_source(
            tenant=p.tenant, name=body.name, url=body.url, transport=body.transport,
            kind=body.kind, onvif=body.onvif, tags=body.tags, group_id=body.group_id,
            default_profile=body.default_profile)
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    await request.app.state.auditor.record(
        "source.created", actor=p.subject, tenant=p.tenant,
        resource=f"source:{src.id}", ip=request.client.host if request.client else "")
    return src.public_dict()


@router.get("/sources", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_sources(request: Request, p: Principal = Depends(require_tenant),
                       limit: int = Query(50, ge=1, le=200),
                       after: str | None = Query(None, max_length=512)) -> dict:
    svc = build_stream_service(request)
    rows, cursor = await svc._sources.list_for_tenant(p.tenant, limit=limit, after=after)
    return {"sources": [s.public_dict() for s in rows], "next": cursor}


@router.get("/sources/{source_id}", dependencies=[Depends(_READ)])
async def get_source(request: Request, source_id: str, p: Principal = Depends(require_tenant)) -> dict:
    try:
        src = await build_stream_service(request).get_source(source_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return src.public_dict()


@router.post("/sources/{source_id}/probe", dependencies=[Depends(_READ), Depends(rate_limit)])
async def probe_source(request: Request, source_id: str,
                       p: Principal = Depends(require_tenant)) -> dict:
    svc = build_stream_service(request)
    try:
        probe = await svc.probe_source(source_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    await request.app.state.auditor.record(
        "source.probed", actor=p.subject, tenant=p.tenant,
        resource=f"source:{source_id}", ip=request.client.host if request.client else "")
    return {"codec": probe.codec, "width": probe.width, "height": probe.height, "fps": probe.fps,
            "has_audio": probe.has_audio, "ptz_capable": probe.ptz_capable, "ok": probe.ok}


class TriggerIn(BaseModel):
    on: str = Field(pattern="^(motion|online|offline)$")
    do: list[str] = Field(default_factory=list)
    debounce_s: int = Field(default=30, ge=0)
    targets: dict = Field(default_factory=dict)


@router.get("/sources/{source_id}/triggers", dependencies=[Depends(_READ)])
async def get_triggers(request: Request, source_id: str,
                       p: Principal = Depends(require_tenant)) -> dict:
    svc = build_stream_service(request)
    try:
        await svc.get_source(source_id, tenant=p.tenant)   # tenant-scope check
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return {"triggers": await TriggerStore(request.app.state.container.datastore).get(source_id)}


@router.put("/sources/{source_id}/triggers", dependencies=[Depends(_MANAGE)])
async def set_triggers(request: Request, source_id: str, body: list[TriggerIn],
                       p: Principal = Depends(require_tenant)) -> dict:
    svc = build_stream_service(request)
    try:
        await svc.get_source(source_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    triggers = [t.model_dump() for t in body]
    await TriggerStore(request.app.state.container.datastore).put(source_id, p.tenant, triggers)
    await request.app.state.auditor.record(
        "triggers.updated", actor=p.subject, tenant=p.tenant,
        resource=f"source:{source_id}", ip=request.client.host if request.client else "")
    return {"triggers": triggers}


@router.delete("/sources/{source_id}", dependencies=[Depends(_MANAGE)])
async def delete_source(request: Request, source_id: str, p: Principal = Depends(require_tenant),
                        force: bool = Query(False)) -> dict:
    svc = build_stream_service(request)
    try:
        src = await svc.get_source(source_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    # Refuse to delete while a session is live unless forced (which stops them first).
    live, _ = await svc._sessions.list_for_tenant(p.tenant, status="live", limit=50)
    live_here = [s for s in live if s.source_id == source_id]
    if live_here and not force:
        raise HTTPException(409, "SESSION_LIVE")
    for s in live_here:
        await svc.stop_session(s.id, tenant=p.tenant, reason="user")
    await svc._sources.delete(source_id)
    await request.app.state.auditor.record(
        "source.deleted", actor=p.subject, tenant=p.tenant,
        resource=f"source:{source_id}", ip=request.client.host if request.client else "")
    return {"deleted": True}


# ── sessions ────────────────────────────────────────────────────────────────────
@router.post("/sessions", status_code=201, dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def start_session(request: Request, body: SessionIn,
                        p: Principal = Depends(require_tenant)) -> dict:
    svc = build_stream_service(request)
    try:
        sess, playback = await svc.start_session(
            tenant=p.tenant, source_id=body.source_id, mode=body.mode,
            outputs=body.outputs, profile=body.profile,
            guards=body.guards.model_dump(exclude_none=True) if body.guards else None)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    except QuotaExceeded as exc:
        raise HTTPException(429, detail={"error": "quota", "dimension": exc.dimension})
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    await request.app.state.auditor.record(
        "session.started", actor=p.subject, tenant=p.tenant,
        resource=f"session:{sess.id}", ip=request.client.host if request.client else "",
        metadata={"source": body.source_id})
    return {"session": sess.public_dict(), "playback": playback}


@router.get("/sessions", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_sessions(request: Request, p: Principal = Depends(require_tenant),
                        limit: int = Query(50, ge=1, le=200),
                        after: str | None = Query(None, max_length=512),
                        session_status: str | None = Query(None, alias="status")) -> dict:
    svc = build_stream_service(request)
    rows, cursor = await svc._sessions.list_for_tenant(p.tenant, limit=limit, after=after,
                                                       status=session_status)
    return {"sessions": [s.public_dict() for s in rows], "next": cursor}


@router.get("/sessions/{session_id}", dependencies=[Depends(_READ)])
async def get_session(request: Request, session_id: str,
                      p: Principal = Depends(require_tenant)) -> dict:
    try:
        sess = await build_stream_service(request).get_session(session_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return sess.public_dict()


@router.post("/sessions/{session_id}/stop", dependencies=[Depends(_MANAGE)])
async def stop_session(request: Request, session_id: str,
                       p: Principal = Depends(require_tenant)) -> dict:
    try:
        sess = await build_stream_service(request).stop_session(session_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    await request.app.state.auditor.record(
        "session.stopped", actor=p.subject, tenant=p.tenant,
        resource=f"session:{session_id}", ip=request.client.host if request.client else "")
    return sess.public_dict()


@router.delete("/sessions/{session_id}", dependencies=[Depends(_MANAGE)])
async def kill_session(request: Request, session_id: str,
                       p: Principal = Depends(require_tenant)) -> dict:
    try:
        sess = await build_stream_service(request).stop_session(session_id, tenant=p.tenant,
                                                                reason="user")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return sess.public_dict()


@router.get("/sessions/{session_id}/playback", dependencies=[Depends(_READ), Depends(rate_limit)])
async def get_playback(request: Request, session_id: str,
                       p: Principal = Depends(require_tenant)) -> dict:
    try:
        return await build_stream_service(request).playback(session_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")


# ── PTZ ────────────────────────────────────────────────────────────────────────
class PtzMove(BaseModel):
    pan: float = Field(default=0.0, ge=-1, le=1)
    tilt: float = Field(default=0.0, ge=-1, le=1)
    zoom: float = Field(default=0.0, ge=-1, le=1)
    mode: str = Field(default="continuous", pattern="^(continuous|relative|absolute)$")


async def _ptz_source(request: Request, source_id: str, tenant: str) -> dict:
    svc = build_stream_service(request)
    try:
        src = await svc.get_source(source_id, tenant=tenant)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    # The plaintext (unsealed) creds/host are needed to reach the camera — never returned to a client.
    # Re-validate + PIN the ONVIF host to its resolved IP at connect time (DNS-rebinding-safe), since
    # this call carries the camera credentials (stream-r2-02).
    return {"onvif": svc.pin_onvif(svc.unseal_onvif(src)),
            "url": svc._sealer.unseal(src.url_sealed, aad=source_aad(src.id))}


@router.post("/sources/{source_id}/ptz/move", dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def ptz_move(request: Request, source_id: str, body: PtzMove,
                   p: Principal = Depends(require_tenant)) -> dict:
    src = await _ptz_source(request, source_id, p.tenant)
    try:
        await request.app.state.container.ptz.move(src, pan=body.pan, tilt=body.tilt,
                                                   zoom=body.zoom, mode=body.mode)
    except NotImplementedError:
        raise HTTPException(409, "PTZ_UNSUPPORTED")
    await request.app.state.auditor.record(
        "ptz.moved", actor=p.subject, tenant=p.tenant,
        resource=f"source:{source_id}", ip=request.client.host if request.client else "",
        metadata={"mode": body.mode})
    return {"moving": True}


@router.post("/sources/{source_id}/ptz/stop", dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def ptz_stop(request: Request, source_id: str, p: Principal = Depends(require_tenant)) -> dict:
    src = await _ptz_source(request, source_id, p.tenant)
    try:
        await request.app.state.container.ptz.stop(src)
    except NotImplementedError:
        raise HTTPException(409, "PTZ_UNSUPPORTED")
    return {"stopped": True}


@router.get("/sources/{source_id}/ptz/presets", dependencies=[Depends(_READ), Depends(rate_limit)])
async def ptz_presets(request: Request, source_id: str,
                      p: Principal = Depends(require_tenant)) -> dict:
    src = await _ptz_source(request, source_id, p.tenant)
    return {"presets": await request.app.state.container.ptz.presets(src)}


@router.post("/sources/{source_id}/ptz/goto/{preset_token}", dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def ptz_goto(request: Request, source_id: str, preset_token: str,
                   p: Principal = Depends(require_tenant)) -> dict:
    src = await _ptz_source(request, source_id, p.tenant)
    try:
        await request.app.state.container.ptz.goto(src, preset_token)
    except NotImplementedError:
        raise HTTPException(409, "PTZ_UNSUPPORTED")
    return {"moving": True}


@router.post("/discovery", dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def discover(request: Request, p: Principal = Depends(require_tenant)) -> dict:
    cams = await request.app.state.container.discovery.discover()
    await request.app.state.auditor.record(
        "discovery.run", actor=p.subject, tenant=p.tenant,
        resource="discovery", ip=request.client.host if request.client else "",
        metadata={"found": str(len(cams))})
    return {"cameras": [{"name": c.name, "url": c.url, "kind": c.kind, "onvif_host": c.onvif_host}
                        for c in cams]}


# ── recordings ────────────────────────────────────────────────────────────────
def _recordings(request: Request) -> RecordingStore:
    return RecordingStore(request.app.state.container.datastore)


@router.get("/recordings", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_recordings(request: Request, p: Principal = Depends(require_tenant),
                          source_id: str | None = Query(None),
                          limit: int = Query(50, ge=1, le=200),
                          after: str | None = Query(None, max_length=512)) -> dict:
    rows, cursor = await _recordings(request).list_for_tenant(
        p.tenant, source_id=source_id, limit=limit, after=after)
    return {"recordings": [r.to_dict() for r in rows], "next": cursor}


async def _load_recording(request: Request, recording_id: str, tenant: str):
    rec = await _recordings(request).get(recording_id)
    if rec is None or rec.tenant != tenant:
        raise HTTPException(404, "NOT_FOUND")   # hidden across tenants
    return rec


@router.get("/recordings/{recording_id}", dependencies=[Depends(_READ)])
async def get_recording(request: Request, recording_id: str,
                        p: Principal = Depends(require_tenant)) -> dict:
    return (await _load_recording(request, recording_id, p.tenant)).to_dict()


@router.post("/recordings/{recording_id}/lock", dependencies=[Depends(_MANAGE)])
async def lock_recording(request: Request, recording_id: str,
                         p: Principal = Depends(require_tenant)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "recording.locked", actor=p.subject, tenant=p.tenant, resource=f"recording:{recording_id}",
        ip=request.client.host if request.client else "")
    rec = await _load_recording(request, recording_id, p.tenant)
    rec.locked = True
    await _recordings(request).save(rec)
    return rec.to_dict()


@router.post("/recordings/{recording_id}/unlock", dependencies=[Depends(_MANAGE)])
async def unlock_recording(request: Request, recording_id: str,
                           p: Principal = Depends(require_tenant)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "recording.unlocked", actor=p.subject, tenant=p.tenant, resource=f"recording:{recording_id}",
        ip=request.client.host if request.client else "")
    rec = await _load_recording(request, recording_id, p.tenant)
    rec.locked = False
    await _recordings(request).save(rec)
    return rec.to_dict()


@router.delete("/recordings/{recording_id}", dependencies=[Depends(_MANAGE)])
async def delete_recording(request: Request, recording_id: str,
                           p: Principal = Depends(require_tenant)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "recording.deleted", actor=p.subject, tenant=p.tenant, resource=f"recording:{recording_id}",
        ip=request.client.host if request.client else "")
    rec = await _load_recording(request, recording_id, p.tenant)
    if rec.locked:
        raise HTTPException(409, "RECORDING_LOCKED")   # legal hold
    c = request.app.state.container
    # stream-006: track per-segment delete outcomes. If ANY blob delete fails, KEEP the recording row
    # (its segment list is the work-list for a retry) and report PARTIAL — deleting the row while footage
    # remained in the bucket orphaned it (unreferenced, undeletable, still billed/retained).
    deleted_segs = failed_segs = 0
    for seg in rec.segments:
        try:
            await c.recording_sink.delete(seg.get("ref", ""))
            deleted_segs += 1
        except Exception:   # noqa: BLE001
            failed_segs += 1
    if failed_segs:
        import logging
        logging.getLogger("app.stream").warning(
            "recording_delete_partial", extra={"recording": recording_id,
                                               "deleted": deleted_segs, "failed": failed_segs})
        return {"deleted": False, "partial": True,
                "segments_deleted": deleted_segs, "segments_failed": failed_segs}
    await _recordings(request).delete(recording_id)
    return {"deleted": True, "segments_deleted": deleted_segs}


# ── clip export ───────────────────────────────────────────────────────────────
def _exports(request: Request) -> ExportService:
    c = request.app.state.container
    s = get_settings()
    return ExportService(
        exports=ExportStore(c.datastore), recordings=RecordingStore(c.datastore),
        exporter=c.clip_exporter, sink=c.recording_sink, queue=c.queue,
        cfg={"max_range_seconds": s.EXPORT_MAX_RANGE_SECONDS, "formats": s.export_formats,
             "max_segments": s.EXPORT_MAX_SEGMENTS, "max_recordings": s.EXPORT_MAX_RECORDINGS,
             "segment_url_ttl": s.EXPORT_SEGMENT_URL_TTL,
             "lease_seconds": s.EXPORT_LEASE_SECONDS})


class ExportIn(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    start: str = Field(min_length=1, max_length=64)
    end: str = Field(min_length=1, max_length=64)
    fmt: str = Field(default="mp4", max_length=16, alias="format")


@router.post("/exports", status_code=201, dependencies=[Depends(_READ), Depends(rate_limit)])
async def request_export(request: Request, body: ExportIn,
                         p: Principal = Depends(require_tenant)) -> dict:
    """Ask for a clip. Returns immediately with a PENDING export; a worker produces it.

    Not synchronous, and that is not a nicety: a two-hour remux is minutes of work, which would
    hold a connection open past every sane timeout and let one export starve the API for everyone.
    """
    s = get_settings()
    if not s.EXPORT_ENABLED:
        # Said plainly rather than accepting a request this platform will never fulfil.
        raise HTTPException(501, "EXPORT_DISABLED")
    # Confirm the source exists and belongs to THIS tenant before creating the export (stream-012):
    # without this a caller could request an export against another tenant's source id (IDOR), matching
    # the ownership check every other source route makes.
    try:
        await build_stream_service(request).get_source(body.source_id, tenant=p.tenant)
    except SourceNotFound:
        raise HTTPException(404, "SOURCE_NOT_FOUND")
    await request.app.state.auditor.record(
        "export.requested", actor=p.subject, tenant=p.tenant,
        resource=f"source:{body.source_id}", ip=request.client.host if request.client else "",
        metadata={"start": body.start, "end": body.end, "format": body.fmt})
    try:
        exp = await _exports(request).request(
            tenant=p.tenant, source_id=body.source_id, start=body.start, end=body.end,
            fmt=body.fmt)
    except ValueError as e:
        # 422: the request is well formed and authorised — the RANGE is the problem, and the
        # message names the limit so a caller can ask for something we will actually do.
        raise HTTPException(422, str(e))
    return exp.public_dict()


@router.get("/exports", dependencies=[Depends(_READ)])
async def list_exports(request: Request, p: Principal = Depends(require_tenant),
                       status_: str | None = Query(None, alias="status", max_length=16),
                       source_id: str | None = Query(None, max_length=128),
                       limit: int = Query(50, ge=1, le=200),
                       after: str | None = Query(None, max_length=512)) -> dict:
    """This tenant's exports.

    There was no listing: an export could be created and then only fetched by an id the caller
    kept. One lost with a browser tab was unreachable — still holding footage and still costing
    storage, invisible to the operator responsible for both.
    """
    rows, cursor = await ExportStore(request.app.state.container.datastore).list_for_tenant(
        p.tenant, limit=limit, after=after, status=status_, source_id=source_id)
    return {"exports": [e.public_dict() for e in rows], "next": cursor}


@router.get("/exports/{export_id}", dependencies=[Depends(_READ)])
async def get_export(request: Request, export_id: str,
                     p: Principal = Depends(require_tenant)) -> dict:
    exp = await ExportStore(request.app.state.container.datastore).get(export_id)
    if exp is None or exp.tenant != (p.tenant):
        raise HTTPException(404, "NOT_FOUND")     # hidden across tenants, like recordings
    return exp.public_dict()


@router.get("/exports/{export_id}/download", dependencies=[Depends(_READ)])
async def download_export(request: Request, export_id: str,
                          p: Principal = Depends(require_tenant)) -> dict:
    """A short-lived link, never the bytes through this process.

    Streaming a multi-gigabyte clip through the API would tie up a worker for the length of the
    download and put footage through a hop that does not need to see it.
    """
    c = request.app.state.container
    exp = await ExportStore(c.datastore).get(export_id)
    if exp is None or exp.tenant != (p.tenant):
        raise HTTPException(404, "NOT_FOUND")
    if exp.status != "ready" or not exp.file_id:
        # 409, not 404: the export exists and is simply not finished. A 404 would send the caller
        # off to create a duplicate of work already in flight.
        raise HTTPException(409, f"EXPORT_{exp.status.upper()}")
    await request.app.state.auditor.record(
        "export.downloaded", actor=p.subject, tenant=p.tenant, resource=f"export:{export_id}",
        ip=request.client.host if request.client else "")
    s = get_settings()
    return {"url": await c.recording_sink.signed_url(exp.file_id, ttl=s.EXPORT_DOWNLOAD_TTL),
            "expires_in": s.EXPORT_DOWNLOAD_TTL, "format": exp.fmt}
