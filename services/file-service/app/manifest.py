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


def _backup() -> dict:
    """This service's backup declaration (app/backup_manifest.py), or an empty one.

    Published in the manifest so the backup fan-out discovers what to capture the same way the DSAR
    fan-out discovers who holds a person — a holder nobody declares is one nobody backs up."""
    try:
        from app.backup_manifest import BACKUP
    except Exception:
        return {"object_prefix": "", "collections": [], "excluded": []}
    return BACKUP


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None,
    large_uploads_available: bool | None = None,
) -> dict[str, Any]:
    # file-013: advertise large uploads from the WIRED object store's actual presign capability, not a
    # driver-name string — so the manifest cannot disagree with the runtime gate (which checks
    # object_store.supports_presign). Fall back to the driver-name heuristic only if the caller cannot
    # pass the live flag (e.g. a manifest built without a container).
    large_available = (settings.OBJECTSTORE_DRIVER == "s3" if large_uploads_available is None
                       else large_uploads_available)
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["files.upload", "files.read", "platform.files", "platform.secrets",
                         "platform.config"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST",
             # DECLARED, because a data map built from manifests is how a DSAR fan-out
             # finds who holds a person. An undeclared holder is never asked, never acks,
             # and is therefore never MISSED — the request closes green on an erasure
             # that did not happen.
             "erasure_category": "subject"},
            {"path": "/internal/files/scan", "tier": "internal", "method": "POST"},
            {"path": "/internal/files/sweep-uploads", "tier": "internal", "method": "POST"},
            # Lets a sibling service hand a user a download without proxying the bytes. Scoped by
            # a REQUIRED tenant argument, not by the internal token alone — that token is held by
            # every service, so on its own it would be a read capability over every tenant.
            {"path": "/internal/files/{file_id}/signed-url", "tier": "internal", "method": "POST"},
            # Who APPEARS IN a file, as distinct from who uploaded it. Without this, data a
            # service stored on a person's behalf names them nowhere, and erasure reports
            # success having matched nothing.
            {"path": "/internal/files/{file_id}/subjects", "tier": "internal", "method": "POST"},            # Backup fan-out (backup-service orchestrates): export + verify + import this
            # service's data. Declared so the fan-out discovers this holder — an undeclared holder
            # is one nobody backs up.
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},

            {"path": "/api/v1/files", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}/metadata", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/{id}/signed-url", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/files/admin/list", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/admin/{id}/metadata", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/uploads", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/uploads/{id}/complete", "tier": "user", "method": "POST"},
            {"path": "/api/v1/files/uploads/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/uploads/{id}", "tier": "user", "method": "DELETE"},
            # The operator surface: credentials and policy an operator changes after launch.
            # A route table built from manifests that omits where credentials are set hides the
            # highest-privilege surface this service has.
            {"path": "/api/v1/files/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/files/operator/secrets/{name}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/files/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/files/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/files/operator/config/{key}", "tier": "user", "method": "DELETE"},
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
        "backup": _backup(),   # what backup-service must capture to restore this service
        "user_actions": _user_actions(),
    }
