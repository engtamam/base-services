"""billing-service — admin actions declared to the platform panel.

Money operations. This lists exactly the routes gated by `_ADMIN` (platform.billing / billing.manage)
— the platform-operator surface. Customer self-service routes (subscribe, change, cancel — gated by
`_SELF` and scoped to the caller's own tenant) are deliberately NOT here: they are the customer's,
not the operator's. Every action re-checks its capability at the route; this file only decides what
the panel SHOWS. See phase-33 and docs/contracts/admin-actions.schema.json.
"""
from __future__ import annotations

from typing import Any

ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Catalog ──────────────────────────────────────────────────────────────
    {
        "id": "billing.create_plan", "label": "Create plan", "method": "POST",
        "path": "/api/v1/billing/plans", "capability": "platform.billing", "group": "Catalog",
    },
    {
        "id": "billing.create_coupon", "label": "Create coupon", "method": "POST",
        "path": "/api/v1/billing/coupons", "capability": "platform.billing", "group": "Catalog",
    },
    # ── Subscriptions (operator) ─────────────────────────────────────────────
    {
        "id": "billing.run_cycle", "label": "Run a billing cycle", "method": "POST",
        "path": "/api/v1/billing/subscriptions/{sub_id}/run", "capability": "platform.billing",
        "group": "Subscriptions", "params": [{"name": "sub_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.migrate_subscription", "label": "Migrate subscription to a new plan",
        "method": "POST", "path": "/api/v1/billing/subscriptions/{sub_id}/migrate",
        "capability": "platform.billing", "group": "Subscriptions", "confirm": True, "params": [{"name": "sub_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.record_overage", "label": "Record overage", "method": "POST",
        "path": "/api/v1/billing/subscriptions/{sub_id}/overage", "capability": "platform.billing",
        "group": "Subscriptions", "params": [{"name": "sub_id", "in": "path", "type": "string", "required": True}],
    },
    # ── Payments ─────────────────────────────────────────────────────────────
    {
        "id": "billing.settle", "label": "Settle invoice (bank transfer / cheque)",
        "method": "POST", "path": "/api/v1/billing/invoices/{invoice_id}/settle",
        "capability": "platform.billing", "group": "Payments", "confirm": True,
        "params": [
            {"name": "invoice_id", "in": "path", "type": "string", "required": True},
            {"name": "method", "in": "body", "type": "string", "required": True,
             "help": "e.g. bank_transfer, cheque"},
            {"name": "issuer", "in": "body", "type": "string", "required": True,
             "help": "the bank / clearing house that minted the reference"},
            {"name": "reference", "in": "body", "type": "string", "required": True,
             "help": "the wire / cheque reference — one transfer can never settle twice"},
            {"name": "amount_minor", "in": "body", "type": "integer", "required": True},
            {"name": "paid_at", "in": "body", "type": "number", "required": True,
             "help": "the bank's VALUE DATE (epoch seconds) — sets the ZATCA tax period"},
        ],
    },
    {
        "id": "billing.reverse_settlement", "label": "Reverse a settlement (recalled transfer)",
        "method": "POST", "path": "/api/v1/billing/settlements/{settlement_id}/reverse",
        "capability": "platform.billing", "group": "Payments", "danger": True, "confirm": True,
        "params": [{"name": "settlement_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.refund", "label": "Refund a payment", "method": "POST",
        "path": "/api/v1/billing/payments/{payment_id}/refund", "capability": "platform.billing",
        "group": "Payments", "danger": True, "confirm": True,
        "params": [{"name": "payment_id", "in": "path", "type": "string", "required": True}],
    },
    # ── Operations ───────────────────────────────────────────────────────────
    {
        "id": "billing.run_dunning", "label": "Run dunning sweep", "method": "POST",
        "path": "/api/v1/billing/dunning/run", "capability": "platform.billing", "group": "Operations",
    },
    {
        "id": "billing.view_ledger", "label": "View the ledger", "method": "GET",
        "path": "/api/v1/billing/ledger", "capability": "platform.billing", "group": "Operations",
    },
    {
        "id": "billing.verify_gateway", "label": "Verify gateway config", "method": "GET",
        "path": "/api/v1/billing/gateway/verify", "capability": "platform.billing", "group": "Operations",
    },
]
