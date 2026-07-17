# =============================================================================
# file-service — Self-describing service manifest
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
    # Large uploads are available only when the wired object store can presign — today that is the
    # s3 driver. Advertised so a CLIENT can discover the boundary and the endpoint up front (the
    # inline route's 413 body is generic, per CWE-209), instead of learning it from an error.
    large_available = settings.OBJECTSTORE_DRIVER == "s3"
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["files.upload", "files.read"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/files/scan", "tier": "internal", "method": "POST"},
            {"path": "/internal/files/sweep-uploads", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/files", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}/metadata", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}/signed-url", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/files/uploads", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/uploads/{id}/complete", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/uploads/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/uploads/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/files/signed/{token}", "tier": "public", "method": "GET"},
        ],
        "features": {
            "large_uploads": {
                "available": large_available,
                "max_inline_bytes": settings.MAX_INLINE_BYTES,
                "max_object_bytes": settings.MAX_OBJECT_BYTES,
                "part_size": settings.MULTIPART_PART_SIZE,
                "initiate": "/api/v1/files/uploads",
            },
        },
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
