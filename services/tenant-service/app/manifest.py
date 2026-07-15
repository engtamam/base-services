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
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),   # what the platform admin panel may render for this service
    }
