# =============================================================================
# backup-service — Self-describing service manifest
# -----------------------------------------------------------------------------
# A machine-readable descriptor so a gateway/platform can discover what this
# service is, what it exposes, which ports (adapters) it uses, and which events
# it emits/consumes. Served at GET /internal/manifest. Phase 1: ports/events are
# empty; later phases populate them.
# =============================================================================
from __future__ import annotations

from typing import Any

from app.config import Settings


def _backup() -> dict:
    """This service's backup declaration (app/backup_manifest.py), or an empty one.

    Published in the manifest so the backup fan-out discovers what to capture the same way the DSAR
    fan-out discovers who holds a person — a holder nobody declares is one nobody backs up."""
    try:
        from app.backup_manifest import BACKUP
    except Exception:
        return {"object_prefix": "", "collections": [], "excluded": []}
    return BACKUP


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
        # FILL THIS. An empty list is a CLAIM — "this service grants no capabilities" — that an
        # adopter's client app reads as fact and renders nothing from. A blank app is the usual
        # symptom of a service that shipped this untouched. If a service genuinely grants none,
        # leaving it empty is correct; the point is that it must be a decision, not a leftover.
        "capabilities": ["platform.secrets", "platform.config", "platform.restore",
                         "platform.backup"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/targets", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/targets", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/targets/{}", "tier": "internal", "method": "DELETE"},
            {"path": "/internal/backup/run", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/runs", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/tenant-coverage", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/health/check", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/prune", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/drill", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/drills", "tier": "internal", "method": "GET"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST",
             # DECLARED, because a data map built from manifests is how a DSAR fan-out
             # finds who holds a person. An undeclared holder is never asked, never acks,
             # and is therefore never MISSED — the request closes green on an erasure
             # that did not happen.
             "erasure_category": "subject"},
            {"path": "/api/v1/admin/ping", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whoami", "tier": "user", "method": "GET"},
            # The operator surface: credentials and policy an operator changes after launch.
            # Declared like anything else — a route table built from manifests that omits the
            # place credentials are set is a route table that hides the highest-privilege
            # surface the service has.
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/config/{}", "tier": "user", "method": "DELETE"},
            # Restore: preview, execute, undo, and the history. The one action that overwrites live
            # data — declared like anything else, under its own platform.restore capability.
            {"path": "/api/v1/operator/restore/plan", "tier": "user", "method": "POST"},
            {"path": "/api/v1/operator/restore", "tier": "user", "method": "POST"},
            {"path": "/api/v1/operator/restore/{}/undo", "tier": "user", "method": "POST"},
            {"path": "/api/v1/operator/restores", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/backup/health", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/backup/drills", "tier": "user", "method": "GET"},
            # Backup management — the full lifecycle a panel drives (platform.backup).
            {"path": "/api/v1/operator/backup/targets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/backup/targets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/backup/targets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/operator/backup/runs", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/backup/run", "tier": "user", "method": "POST"},
            {"path": "/api/v1/operator/backup/drill/run", "tier": "user", "method": "POST"},
            {"path": "/api/v1/operator/backup/prune", "tier": "user", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # FILL THIS too, or leave it empty deliberately. Same trap as capabilities: an empty
        # `emitted` reads as "this service publishes nothing", so a consumer wiring against it
        # subscribes to nothing and fails silently rather than at boot.
        "events": {"emitted": [], "consumed": []},  # e.g. {"emitted": ["wallet.credited"], ...}
        "admin_actions": _admin_actions(),
        "backup": _backup(),
        "user_actions": _user_actions(),
    }
