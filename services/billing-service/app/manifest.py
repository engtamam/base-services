# =============================================================================
# billing-service — Self-describing service manifest
# -----------------------------------------------------------------------------
# A machine-readable descriptor so a gateway/platform can discover what this
# service is, what it exposes, which ports (adapters) it uses, and which events
# it emits/consumes. Served at GET /internal/manifest. Phase 1: ports/events are
# empty; later phases populate them.
# =============================================================================
from __future__ import annotations

from typing import Any

from app.core.events import EMITTED as _EMITTED

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
        "capabilities": ["platform.billing", "billing.manage", "billing.self",
                         "platform.secrets", "platform.config"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},
            {"path": "/internal/privacy/erase", "tier": "internal", "method": "POST",
             # DECLARED, because a data map built from manifests is how a DSAR fan-out
             # finds who holds a person. An undeclared holder is never asked, never acks,
             # and is therefore never MISSED — the request closes green on an erasure
             # that did not happen.
             "erasure_category": "subject"},
            {"path": "/internal/billing/webhook/{provider}", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/dunning", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/billing/plans", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/plans", "tier": "user", "method": "GET"},
            # PUBLIC by design: a pricing page has no principal. Shows only listed+active plans,
            # projected to a whitelist of offer fields.
            {"path": "/api/v1/billing/public/plans", "tier": "public", "method": "GET"},
            {"path": "/api/v1/billing/offers", "tier": "public", "method": "GET"},
            {"path": "/api/v1/billing/coupons", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/checkouts", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/subscriptions/{id}/change", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/pause", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/reactivate", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/cancel", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/run", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/migrate", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/overage", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/entitlements/{key}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/usage", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/invoices/{invoice_id}/settle", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/invoices/{invoice_id}/settlements", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/settlements/{settlement_id}/reverse", "tier": "user", "method": "POST"},
            # Advisory read. ENFORCEMENT is /internal/billing/usage/consume (atomic).
            {"path": "/internal/billing/entitlements/check", "tier": "internal", "method": "GET"},
            {"path": "/internal/billing/usage/consume", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/usage/release", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/usage/set", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/usage", "tier": "internal", "method": "GET"},
            {"path": "/internal/billing/grants/drain", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/grants/reconcile", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/grants/reconcile-clawbacks", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/cycle-due", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/billing/ledger", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/gateway/verify", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/usage", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/invoices", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/admin/invoices", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/invoices/{id}/pdf", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/invoices/{id}/receipt", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/invoices/{id}/document", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/payments/{id}/refund", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/dunning/run", "tier": "user", "method": "POST"},
            # The operator surface: payment credentials and policy, changeable after launch.
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/operator/config/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/billing/plans/{}", "tier": "user", "method": "PATCH"},
            {"path": "/api/v1/billing/plans/{}/availability", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/coupons", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/coupons/{}", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/coupons/{}", "tier": "user", "method": "PATCH"},
            {"path": "/api/v1/billing/coupons/{}", "tier": "user", "method": "DELETE"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # DECLARED, because a consumer cannot subscribe to an event it cannot discover. An
        # undeclared event reaches nobody, which is the same as not publishing it — and the
        # safety:event-contract gate fails the build if this list and core/events.py disagree.
        "events": {"emitted": list(_EMITTED), "consumed": []},
        "admin_actions": _admin_actions(),
        "backup": _backup(),
        "user_actions": _user_actions(),
    }
