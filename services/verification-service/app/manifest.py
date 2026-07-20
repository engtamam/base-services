# =============================================================================
# verification-service — Self-describing service manifest
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
        # The capabilities a panel may advertise. `verification.self` is the end-user's own-claim
        # capability (the <svc>.self pattern); the other two gate the review queue, which is where
        # trust is actually granted.
        "capabilities": ["platform.verification", "verification.review", "verification.read",
                         "verification.self"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST"},
            {"path": "/internal/verification/identity/{claim_ref}/seen", "tier": "internal",
             "method": "GET"},
            {"path": "/internal/verification/sweep-pending", "tier": "internal", "method": "POST"},
            # ── user tier: a person checking their OWN claim ─────────────────
            {"path": "/api/v1/verification/claims", "tier": "user", "method": "POST"},
            {"path": "/api/v1/verification/claims", "tier": "user", "method": "GET"},
            {"path": "/api/v1/verification/status", "tier": "user", "method": "GET"},
            # ── admin tier: the review queue, where trust is granted ─────────
            {"path": "/api/v1/verification/review-queue", "tier": "user", "method": "GET"},
            {"path": "/api/v1/verification/review/{verification_id}", "tier": "user",
             "method": "POST"},
            {"path": "/api/v1/admin/ping", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whoami", "tier": "user", "method": "GET"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": ["verification.settled"], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
