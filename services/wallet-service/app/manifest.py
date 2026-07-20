# =============================================================================
# wallet-service — Self-describing service manifest
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
        "capabilities": ["platform.wallet", "wallet.manage", "wallet.read"],
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
            {"path": "/internal/wallet/{tenant}", "tier": "internal", "method": "GET"},
            {"path": "/internal/wallet/{tenant}/reconcile", "tier": "internal", "method": "GET"},
            {"path": "/internal/wallet/credit", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/spend", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/reserve", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/confirm", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/release", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/refund", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/sweep-holds", "tier": "internal", "method": "POST"},
            {"path": "/internal/wallet/sweep-grants", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/wallet", "tier": "user", "method": "GET"},
            {"path": "/api/v1/wallet/ledger", "tier": "user", "method": "GET"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # OPTIONAL consumer: billing invoices these IF it listens. Wallet stands alone.
        "events": {"emitted": ["wallet.credited", "wallet.spent"], "consumed": []},
        "admin_actions": _admin_actions(),
        "user_actions": _user_actions(),
    }
