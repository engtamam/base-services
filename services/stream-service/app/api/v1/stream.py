# =============================================================================
# stream-service — user tier: sources, sessions, playback
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.sealing import redact_url
from app.core.ssrf import SsrfBlocked
from app.core.recording import RecordingShipper
from app.core.stores import RecordingStore, SessionStore, SourceStore
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
        playback_ttl=s.PLAYBACK_URL_TTL_SECONDS, output_root=s.OBJECTSTORE_ROOT + "/hls")
    shipper = RecordingShipper(sink=c.recording_sink, recordings=RecordingStore(c.datastore))
    return StreamService(
        sources=SourceStore(c.datastore), sessions=SessionStore(c.datastore),
        placer=c.session_placer, engine=c.stream_engine, sealer=c.sealer,
        signed_playback=c.signed_playback, cfg=cfg, events=c.event_bus,
        shipper=shipper, record_ext=_REC_EXT.get(s.RECORD_FORMAT, "mp4"))


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
                          p: Principal = Depends(require_user)) -> dict:
    svc = build_stream_service(request)
    try:
        src = await svc.register_source(
            tenant=p.tenant or "", name=body.name, url=body.url, transport=body.transport,
            kind=body.kind, onvif=body.onvif, tags=body.tags, group_id=body.group_id,
            default_profile=body.default_profile)
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    return src.public_dict()


@router.get("/sources", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_sources(request: Request, p: Principal = Depends(require_user),
                       limit: int = Query(50, ge=1, le=200),
                       after: str | None = Query(None, max_length=512)) -> dict:
    svc = build_stream_service(request)
    rows, cursor = await svc._sources.list_for_tenant(p.tenant or "", limit=limit, after=after)
    return {"sources": [s.public_dict() for s in rows], "next": cursor}


@router.get("/sources/{source_id}", dependencies=[Depends(_READ)])
async def get_source(request: Request, source_id: str, p: Principal = Depends(require_user)) -> dict:
    try:
        src = await build_stream_service(request).get_source(source_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return src.public_dict()


@router.post("/sources/{source_id}/probe", dependencies=[Depends(_READ), Depends(rate_limit)])
async def probe_source(request: Request, source_id: str,
                       p: Principal = Depends(require_user)) -> dict:
    svc = build_stream_service(request)
    try:
        probe = await svc.probe_source(source_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    return {"codec": probe.codec, "width": probe.width, "height": probe.height, "fps": probe.fps,
            "has_audio": probe.has_audio, "ptz_capable": probe.ptz_capable, "ok": probe.ok}


@router.delete("/sources/{source_id}", dependencies=[Depends(_MANAGE)])
async def delete_source(request: Request, source_id: str, p: Principal = Depends(require_user),
                        force: bool = Query(False)) -> dict:
    svc = build_stream_service(request)
    try:
        src = await svc.get_source(source_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    # Refuse to delete while a session is live unless forced (which stops them first).
    live, _ = await svc._sessions.list_for_tenant(p.tenant or "", status="live", limit=50)
    live_here = [s for s in live if s.source_id == source_id]
    if live_here and not force:
        raise HTTPException(409, "SESSION_LIVE")
    for s in live_here:
        await svc.stop_session(s.id, tenant=p.tenant or "", reason="user")
    await svc._sources.delete(source_id)
    return {"deleted": True}


# ── sessions ────────────────────────────────────────────────────────────────────
@router.post("/sessions", status_code=201, dependencies=[Depends(_MANAGE), Depends(rate_limit)])
async def start_session(request: Request, body: SessionIn,
                        p: Principal = Depends(require_user)) -> dict:
    svc = build_stream_service(request)
    try:
        sess, playback = await svc.start_session(
            tenant=p.tenant or "", source_id=body.source_id, mode=body.mode,
            outputs=body.outputs, profile=body.profile,
            guards=body.guards.model_dump(exclude_none=True) if body.guards else None)
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    except QuotaExceeded as exc:
        raise HTTPException(429, detail={"error": "quota", "dimension": exc.dimension})
    except SsrfBlocked:
        raise HTTPException(400, "SOURCE_URL_BLOCKED")
    return {"session": sess.public_dict(), "playback": playback}


@router.get("/sessions", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_sessions(request: Request, p: Principal = Depends(require_user),
                        limit: int = Query(50, ge=1, le=200),
                        after: str | None = Query(None, max_length=512),
                        session_status: str | None = Query(None, alias="status")) -> dict:
    svc = build_stream_service(request)
    rows, cursor = await svc._sessions.list_for_tenant(p.tenant or "", limit=limit, after=after,
                                                       status=session_status)
    return {"sessions": [s.public_dict() for s in rows], "next": cursor}


@router.get("/sessions/{session_id}", dependencies=[Depends(_READ)])
async def get_session(request: Request, session_id: str,
                      p: Principal = Depends(require_user)) -> dict:
    try:
        sess = await build_stream_service(request).get_session(session_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return sess.public_dict()


@router.post("/sessions/{session_id}/stop", dependencies=[Depends(_MANAGE)])
async def stop_session(request: Request, session_id: str,
                       p: Principal = Depends(require_user)) -> dict:
    try:
        sess = await build_stream_service(request).stop_session(session_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return sess.public_dict()


@router.delete("/sessions/{session_id}", dependencies=[Depends(_MANAGE)])
async def kill_session(request: Request, session_id: str,
                       p: Principal = Depends(require_user)) -> dict:
    try:
        sess = await build_stream_service(request).stop_session(session_id, tenant=p.tenant or "",
                                                                reason="user")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")
    return sess.public_dict()


@router.get("/sessions/{session_id}/playback", dependencies=[Depends(_READ)])
async def get_playback(request: Request, session_id: str,
                       p: Principal = Depends(require_user)) -> dict:
    try:
        return await build_stream_service(request).playback(session_id, tenant=p.tenant or "")
    except SourceNotFound:
        raise HTTPException(404, "NOT_FOUND")


# ── recordings ────────────────────────────────────────────────────────────────
def _recordings(request: Request) -> RecordingStore:
    return RecordingStore(request.app.state.container.datastore)


@router.get("/recordings", dependencies=[Depends(_READ), Depends(rate_limit)])
async def list_recordings(request: Request, p: Principal = Depends(require_user),
                          source_id: str | None = Query(None),
                          limit: int = Query(50, ge=1, le=200),
                          after: str | None = Query(None, max_length=512)) -> dict:
    rows, cursor = await _recordings(request).list_for_tenant(
        p.tenant or "", source_id=source_id, limit=limit, after=after)
    return {"recordings": [r.to_dict() for r in rows], "next": cursor}


async def _load_recording(request: Request, recording_id: str, tenant: str):
    rec = await _recordings(request).get(recording_id)
    if rec is None or rec.tenant != tenant:
        raise HTTPException(404, "NOT_FOUND")   # hidden across tenants
    return rec


@router.get("/recordings/{recording_id}", dependencies=[Depends(_READ)])
async def get_recording(request: Request, recording_id: str,
                        p: Principal = Depends(require_user)) -> dict:
    return (await _load_recording(request, recording_id, p.tenant or "")).to_dict()


@router.post("/recordings/{recording_id}/lock", dependencies=[Depends(_MANAGE)])
async def lock_recording(request: Request, recording_id: str,
                         p: Principal = Depends(require_user)) -> dict:
    rec = await _load_recording(request, recording_id, p.tenant or "")
    rec.locked = True
    await _recordings(request).save(rec)
    return rec.to_dict()


@router.post("/recordings/{recording_id}/unlock", dependencies=[Depends(_MANAGE)])
async def unlock_recording(request: Request, recording_id: str,
                           p: Principal = Depends(require_user)) -> dict:
    rec = await _load_recording(request, recording_id, p.tenant or "")
    rec.locked = False
    await _recordings(request).save(rec)
    return rec.to_dict()


@router.delete("/recordings/{recording_id}", dependencies=[Depends(_MANAGE)])
async def delete_recording(request: Request, recording_id: str,
                           p: Principal = Depends(require_user)) -> dict:
    rec = await _load_recording(request, recording_id, p.tenant or "")
    if rec.locked:
        raise HTTPException(409, "RECORDING_LOCKED")   # legal hold
    c = request.app.state.container
    for seg in rec.segments:
        try:
            await c.recording_sink.delete(seg.get("ref", ""))
        except Exception:   # noqa: BLE001
            pass
    await _recordings(request).delete(recording_id)
    return {"deleted": True}
