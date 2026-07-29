# =============================================================================
# tenant-service — Self-describing service manifest
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
        "capabilities": ["platform.tenants", "tenant.manage"],
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
            {"path": "/internal/tenants/close-sweep", "tier": "internal", "method": "POST"},
            {"path": "/internal/tenants/resolve", "tier": "internal", "method": "GET"},
            {"path": "/internal/tenants/{id}", "tier": "internal", "method": "GET"},
            {"path": "/api/v1/tenants", "tier": "user", "method": "POST"},
            {"path": "/api/v1/tenants", "tier": "user", "method": "GET"},
            {"path": "/api/v1/tenants/{id}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/tenants/{id}/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/tenants/{id}/branding", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/tenants/{id}/suspend", "tier": "user", "method": "POST"},
            {"path": "/api/v1/tenants/{id}/reactivate", "tier": "user", "method": "POST"},
            {"path": "/api/v1/tenants/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/tenants/{id}/export", "tier": "user", "method": "GET"},
            {"path": "/api/v1/tenant", "tier": "user", "method": "GET"},
            {"path": "/api/v1/tenant/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/tenant/branding", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/tenant/close", "tier": "user", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),   # what the platform admin panel may render for this service
        "user_actions": _user_actions(),
    }
