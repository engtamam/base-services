# =============================================================================
# webhook-service — Self-describing service manifest
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
        "capabilities": ["platform.webhooks", "webhooks.manage", "webhooks.read"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/webhooks/dispatch", "tier": "internal", "method": "POST"},
            {"path": "/internal/webhooks/deliver/{id}", "tier": "internal", "method": "POST"},
            {"path": "/internal/webhooks/sweep", "tier": "internal", "method": "POST"},
            {"path": "/inbound/{source}", "tier": "public", "method": "POST"},
            {"path": "/api/v1/webhooks/endpoints", "tier": "user", "method": "POST"},
            {"path": "/api/v1/webhooks/endpoints", "tier": "user", "method": "GET"},
            {"path": "/api/v1/webhooks/endpoints/{id}", "tier": "user", "method": "PATCH"},
            {"path": "/api/v1/webhooks/endpoints/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/webhooks/endpoints/{id}/rotate-secret", "tier": "user", "method": "POST"},
            {"path": "/api/v1/webhooks/endpoints/{id}/health", "tier": "user", "method": "GET"},
            {"path": "/api/v1/webhooks/deliveries", "tier": "user", "method": "GET"},
            {"path": "/api/v1/webhooks/dead-letters", "tier": "user", "method": "GET"},
            {"path": "/api/v1/webhooks/deliveries/{id}/replay", "tier": "user", "method": "POST"},
            {"path": "/api/v1/webhooks/events/{id}/replay", "tier": "user", "method": "POST"},
            {"path": "/api/v1/webhooks/dispatch", "tier": "user", "method": "POST"},
            {"path": "/api/v1/webhooks/sweep", "tier": "user", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
