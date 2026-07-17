# =============================================================================
# stream-service — Self-describing service manifest
# -----------------------------------------------------------------------------
# A machine-readable descriptor so a gateway/platform can discover what this
# service is, what it exposes, which ports (adapters) it uses, and which events
# it emits/consumes. Served at GET /internal/manifest. Phase 1: ports/events are
# empty; later phases populate them.
# =============================================================================
from __future__ import annotations

from typing import Any

from app.config import Settings


def _admin_actions() -> list[dict[str, Any]]:
    """This service's declared admin actions (docs/contracts/admin-actions.schema.json), or [] if it
    declares none. Opt-in: a service with no app/admin_actions.py simply exposes nothing to the
    panel. The panel is a map — these decide what is SHOWN, never what an operator may DO."""
    try:
        from app.admin_actions import ADMIN_ACTIONS
    except Exception:
        return []
    return list(ADMIN_ACTIONS)


def _user_actions() -> list[dict[str, Any]]:
    """This service's declared end-user (self-service) actions
    (docs/contracts/user-actions.schema.json), or [] if it declares none. Opt-in: aggregated into
    GET /api/v1/app/catalog. The app is a map — these decide what is SHOWN, never what a user may DO."""
    try:
        from app.user_actions import USER_ACTIONS
    except Exception:
        return []
    return list(USER_ACTIONS)


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["platform.stream", "stream.manage", "stream.view"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/stream/sweep", "tier": "internal", "method": "POST"},
            {"path": "/internal/stream/retention", "tier": "internal", "method": "POST"},
            {"path": "/internal/stream/sessions/{id}/heartbeat", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/sources", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sources", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sources/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sources/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/sources/{id}/probe", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sources/{id}/triggers", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sources/{id}/triggers", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/sessions", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sessions", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sessions/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sessions/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/sessions/{id}/stop", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sessions/{id}/playback", "tier": "user", "method": "GET"},
            {"path": "/api/v1/recordings", "tier": "user", "method": "GET"},
            {"path": "/api/v1/recordings/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/recordings/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/recordings/{id}/lock", "tier": "user", "method": "POST"},
            {"path": "/api/v1/recordings/{id}/unlock", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sources/{id}/ptz/move", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sources/{id}/ptz/stop", "tier": "user", "method": "POST"},
            {"path": "/api/v1/sources/{id}/ptz/presets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/sources/{id}/ptz/goto/{token}", "tier": "user", "method": "POST"},
            {"path": "/api/v1/discovery", "tier": "user", "method": "POST"},
        ],
        "features": {
            "live": {
                "hls": {"segment_seconds": settings.HLS_SEGMENT_SECONDS,
                        "window_segments": settings.HLS_WINDOW_SEGMENTS,
                        "low_latency": settings.HLS_LOW_LATENCY},
                "webrtc": {"enabled": settings.WEBRTC_ENABLED,
                           "max_viewers_per_session": settings.WEBRTC_MAX_VIEWERS_PER_SESSION},
                "snapshot": {"min_interval_seconds": settings.SNAPSHOT_MIN_INTERVAL_SECONDS,
                             "thumbnail_interval_seconds": settings.THUMBNAIL_INTERVAL_SECONDS,
                             "max_width": settings.SNAPSHOT_MAX_WIDTH},
            },
            "recording": {"segment_seconds": settings.RECORD_SEGMENT_SECONDS,
                          "on_start": settings.RECORD_ON_START,
                          "transcode_profiles": settings.transcode_profiles,
                          "retention_days": settings.RETENTION_DAYS,
                          "retention_max_bytes": settings.RETENTION_MAX_BYTES,
                          "export_max_range_seconds": settings.EXPORT_MAX_RANGE_SECONDS},
            "quota": {"max_concurrent_per_tenant": settings.STREAM_MAX_CONCURRENT_SESSIONS_PER_TENANT,
                      "max_hours_month": settings.STREAM_MAX_HOURS_MONTH,
                      "max_storage_bytes": settings.STREAM_MAX_STORAGE_BYTES},
            "health": {"reconnect_max": settings.RECONNECT_MAX,
                       "reconnect_backoff_ms": settings.RECONNECT_BACKOFF_MS,
                       "offline_after_seconds": settings.SOURCE_OFFLINE_AFTER_SECONDS,
                       "probe_ttl_seconds": settings.SOURCE_PROBE_TTL_SECONDS,
                       "start_timeout_seconds": settings.SESSION_START_TIMEOUT_SECONDS},
            "session": {"default_mode": settings.SESSION_DEFAULT_MODE,
                        "schedule_timezone": settings.SCHEDULE_TIMEZONE_DEFAULT,
                        "signed_playback": settings.SIGNED_PLAYBACK_DRIVER},
        },
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": ["stream.session.started", "stream.session.stopped",
                               "stream.motion.detected"], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
