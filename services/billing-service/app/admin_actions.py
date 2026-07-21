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
        # The read that makes the reversal usable: you must SEE an invoice's settlements before you
        # can pick one to reverse.
        "id": "billing.list_settlements", "label": "View invoice settlements",
        "method": "GET", "path": "/api/v1/billing/invoices/{invoice_id}/settlements",
        "capability": "platform.billing", "group": "Settlement",
        "params": [{"name": "invoice_id", "in": "path", "type": "string", "required": True}],
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
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Provider credentials", "method": "GET",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a provider credential", "method": "PUT",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. TAQNYAT_TOKEN or RESEND_API_KEY"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a provider credential", "method": "DELETE",
        "path": "/api/v1/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/operator/config", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "e.g. EMAIL_CHAIN to reorder providers, or SMS_CHAIN"},
            {"name": "value", "in": "body", "type": "string", "required": True},
            {"name": "tenant", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
        "path": "/api/v1/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.update_plan", "label": "Edit a plan", "method": "PATCH",
        "path": "/api/v1/billing/plans/{plan_id}", "capability": "billing.manage",
        "group": "Catalogue", "confirm": True,
        "params": [
            {"name": "plan_id", "in": "path", "type": "string", "required": True},
            {"name": "name", "in": "body", "type": "string", "required": False},
            {"name": "prices", "in": "body", "type": "string", "required": False,
             "help": "existing subscribers keep the price they were sold"},
            {"name": "limits", "in": "body", "type": "string", "required": False},
            {"name": "trial_days", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.plan_availability", "label": "Retire or hide a plan", "method": "POST",
        "path": "/api/v1/billing/plans/{plan_id}/availability", "capability": "billing.manage",
        "group": "Catalogue", "confirm": True,
        "params": [
            {"name": "plan_id", "in": "path", "type": "string", "required": True},
            {"name": "active", "in": "body", "type": "string", "required": False,
             "help": "false: nobody new may subscribe. Existing subscribers are untouched"},
            {"name": "listed", "in": "body", "type": "string", "required": False,
             "help": "false: hidden from the plan list, still subscribable"},
        ],
    }
]
