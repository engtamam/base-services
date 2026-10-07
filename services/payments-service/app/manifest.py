# =============================================================================
# payments-service — Self-describing service manifest
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
        # The two the scaffold's OWN operator routes already enforce (api/v1/operator.py:
        # require_capability("platform.secrets") / ("platform.config")) — pre-declared so a freshly
        # generated service's operator routes are grantable and reachable, not permanently dark until
        # someone remembers to advertise them. ADD this service's domain capabilities alongside them
        # (e.g. "files.upload", "files.read"); remove one only if you also remove the route that
        # enforces it. An advertised capability is what the admin panel and onboarding grant against.
        "capabilities": ["platform.secrets", "platform.config"],
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
            {"path": "/api/v1/admin/ping", "tier": "user", "method": "GET"},
            {"path": "/api/v1/whoami", "tier": "user", "method": "GET"},
            # The operator surface: credentials and policy an operator changes after launch.
            # Declared like anything else — a route table built from manifests that omits the
            # place credentials are set is a route table that hides the highest-privilege
            # surface the service has.
            {"path": "/api/v1/app/operator/secrets", "tier": "user", "method": "GET"},
            {"path": "/api/v1/app/operator/secrets", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/app/operator/secrets/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/app/operator/config", "tier": "user", "method": "GET"},
            {"path": "/api/v1/app/operator/config", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/app/operator/config/{}", "tier": "user", "method": "DELETE"},
            # Per-shop commerce option toggles (external pay-link enable/lock) — operator surface (P6).
            {"path": "/api/v1/app/operator/options", "tier": "user", "method": "GET"},
            {"path": "/api/v1/app/operator/options", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/app/operator/options/{}", "tier": "user", "method": "DELETE"},
            {"path": "/api/v1/app/operator/payment-mode", "tier": "user", "method": "GET"},
            {"path": "/api/v1/app/operator/payment-mode", "tier": "user", "method": "PUT"},
            {"path": "/api/v1/app/operator/sub-merchants/{}", "tier": "user", "method": "GET"},
            # PUBLIC (unauthenticated by design): the hosted pay-link redirector the customer's browser
            # follows, and the gateway's server-to-server webhook (adapter-verified, tenant in the path).
            {"path": "/api/v1/payments/pay/{}", "tier": "public", "method": "GET"},
            {"path": "/api/v1/payments/webhook/{}", "tier": "public", "method": "POST"},
            {"path": "/api/v1/payments/payout-webhook", "tier": "public", "method": "POST"},
            # Internal peer tier (a product service acting for a shop): sealed merchant credentials
            # (write-only; no read route) and the server-authoritative PaymentRequest lifecycle.
            {"path": "/internal/payments/merchant-credentials", "tier": "internal", "method": "GET"},
            {"path": "/internal/payments/merchant-credentials", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/merchant-credentials/clear", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/marketplace-credentials", "tier": "internal", "method": "GET"},
            {"path": "/internal/payments/marketplace-credentials", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/marketplace-credentials/clear", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/requests", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/requests/{}", "tier": "internal", "method": "GET"},
            {"path": "/internal/payments/requests/{}/reconcile", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/sub-merchants/onboard", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/sub-merchants/destination", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/sub-merchants/{}", "tier": "internal", "method": "GET"},
            {"path": "/internal/payments/payment-mode", "tier": "internal", "method": "POST"},
            {"path": "/internal/payments/payment-mode", "tier": "internal", "method": "GET"},
            {"path": "/internal/payments/payouts/reconcile-sweep", "tier": "internal", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        # FILL THIS too, or leave it empty deliberately. Same trap as capabilities: an empty
        # `emitted` reads as "this service publishes nothing", so a consumer wiring against it
        # subscribes to nothing and fails silently rather than at boot.
        # payments-004: PaymentService._emit publishes these on every settled request, so a consumer
        # (billing, wallet) discovers the fan-out here rather than by reading the source.
        "events": {"emitted": ["payment.completed", "payment.failed"], "consumed": []},
        "admin_actions": _admin_actions(),
        "backup": _backup(),
        "user_actions": _user_actions(),
    }
