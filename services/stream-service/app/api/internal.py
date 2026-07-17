# =============================================================================
# stream-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.config import Settings, get_settings
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
    return await build_stream_service(request).enforce_and_reconcile()


class HeartbeatIn(BaseModel):
    stats: dict = {}


@router.post("/stream/sessions/{session_id}/heartbeat", dependencies=[Depends(require_internal_token)])
async def heartbeat(request: Request, session_id: str, body: HeartbeatIn) -> dict:
    """Engine → service: extend the session lease and record stats (fps/bitrate/bytes). The size
    guard is checked here, so a recording that hit its cap stops immediately."""
    from app.api.v1.stream import build_stream_service
    sess = await build_stream_service(request).heartbeat(session_id, stats=body.stats or None)
    return sess.public_dict() if sess is not None else {"status": "gone"}


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
