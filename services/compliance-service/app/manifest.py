# =============================================================================
# compliance-service — Self-describing service manifest
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
        "capabilities": ["platform.compliance", "compliance.manage", "compliance.read"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/compliance/consent-check", "tier": "internal", "method": "GET"},
            {"path": "/internal/compliance/hold-check", "tier": "internal", "method": "GET"},
            {"path": "/internal/compliance/dsar/{id}/target-ack", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/consent/sweep-lapses", "tier": "internal", "method": "POST"},
            {"path": "/internal/compliance/consent/migrate-purpose-keys", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/compliance/consent/grant", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/consent/withdraw", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/consent/history", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/dsar", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/dsar", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/dsar/{id}/verify", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/dsar/{id}/process", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/legal-holds", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/legal-holds", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/legal-holds/{id}/release", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/data-targets", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/data-targets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/data-targets/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/compliance/data-targets/unreachable", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/ropa", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/ropa", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/ropa/{id}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/compliance/breaches", "tier": "user", "method": "POST"},
            {"path": "/api/v1/compliance/breaches", "tier": "user", "method": "GET"},
            {"path": "/api/v1/compliance/breaches/{id}/notify", "tier": "user", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
