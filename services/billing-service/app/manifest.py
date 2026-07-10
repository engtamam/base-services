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

from app.config import Settings


def build_manifest(
    settings: Settings, ports: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "name": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env": settings.ENV,
        "capabilities": ["platform.billing", "billing.manage", "billing.self"],
        "endpoints": [
            {"path": "/healthz", "tier": "public", "method": "GET"},
            {"path": "/readyz", "tier": "public", "method": "GET"},
            {"path": "/metrics", "tier": "public", "method": "GET"},
            {"path": "/internal/health", "tier": "internal", "method": "GET"},
            {"path": "/internal/manifest", "tier": "internal", "method": "GET"},
            {"path": "/internal/billing/webhook/{provider}", "tier": "internal", "method": "POST"},
            {"path": "/internal/billing/dunning", "tier": "internal", "method": "POST"},
            {"path": "/api/v1/billing/plans", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/plans", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/coupons", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/subscriptions/{id}/change", "tier": "user", "method": "POST"},
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
            {"path": "/api/v1/billing/ledger", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/gateway/verify", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/usage", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/invoices", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/invoices/{id}/pdf", "tier": "user", "method": "GET"},
            {"path": "/api/v1/billing/payments/{id}/refund", "tier": "user", "method": "POST"},
            {"path": "/api/v1/billing/dunning/run", "tier": "user", "method": "POST"},
        ],
        "ports": ports or [],  # port -> driver, from the composition root
        "events": {"emitted": [], "consumed": []},
    }
