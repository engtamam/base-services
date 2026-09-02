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
    # A panel that can only ACT is half a panel. Plan listing existed ONLY under billing.self,
    # so a CUSTOMER could see the catalogue and an OPERATOR could not — they had a button to
    # change a price and no way to see what the price was.
    {
        "id": "billing.list_plans", "label": "Plans & pricing", "method": "GET",
        "path": "/api/v1/billing/plans", "capability": "billing.manage", "group": "Catalogue",
        "params": [
            {"name": "include_hidden", "in": "query", "type": "boolean", "required": False,
             "help": "also show retired and unlisted plans — the operator view"},
        ],
    },
    {
        "id": "billing.platform_invoices", "label": "Invoices (all tenants)", "method": "GET",
        "path": "/api/v1/billing/admin/invoices", "capability": "platform.billing",
        "group": "Invoices",
        "params": [
            {"name": "status", "in": "query", "type": "string", "required": False,
             "help": "at least one filter is required — open | paid | void | uncollectible"},
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "narrow to one customer"},
            {"name": "since", "in": "query", "type": "string", "required": False,
             "help": "issue-date lower bound, e.g. 2026-07-01"},
            {"name": "until", "in": "query", "type": "string", "required": False},
            {"name": "subscription_id", "in": "query", "type": "string", "required": False},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
            {"name": "cursor", "in": "query", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.platform_subscriptions", "label": "Subscriptions (all tenants)",
        "method": "GET", "path": "/api/v1/billing/admin/subscriptions",
        "capability": "platform.billing", "group": "Subscriptions",
        "params": [
            {"name": "status", "in": "query", "type": "string", "required": False,
             "help": "at least one filter is required — trialing | active | past_due | paused | canceled"},
            {"name": "plan_id", "in": "query", "type": "string", "required": False},
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "narrow to one customer"},
            {"name": "since", "in": "query", "type": "string", "required": False,
             "help": "creation-date lower bound, e.g. 2026-07-01"},
            {"name": "until", "in": "query", "type": "string", "required": False},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
            {"name": "cursor", "in": "query", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.list_invoices", "label": "Invoices", "method": "GET",
        "path": "/api/v1/billing/invoices", "capability": "billing.manage", "group": "Invoices",
        "params": [
            {"name": "status", "in": "query", "type": "string", "required": False,
             "help": "open | paid | void | uncollectible"},
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "platform.billing only: look across tenants"},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
            {"name": "cursor", "in": "query", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.list_subscriptions", "label": "Subscriptions", "method": "GET",
        "path": "/api/v1/billing/subscriptions", "capability": "billing.manage",
        "group": "Subscriptions",
        "params": [
            {"name": "status", "in": "query", "type": "string", "required": False},
            {"name": "plan_id", "in": "query", "type": "string", "required": False},
            {"name": "tenant", "in": "query", "type": "string", "required": False,
             "help": "platform.billing only: look across tenants"},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
            {"name": "cursor", "in": "query", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.create_plan", "label": "Create plan", "method": "POST",
        "path": "/api/v1/billing/plans", "capability": "platform.billing", "group": "Catalog",
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True},
            {"name": "prices", "in": "body", "type": "array", "required": True,
             "help": "[{interval, amount_minor, currency, type}] — the API rejects an empty list"},
            {"name": "features", "in": "body", "type": "array", "required": False},
            {"name": "limits", "in": "body", "type": "object", "required": False},
            {"name": "trial_days", "in": "body", "type": "integer", "required": False},
        ],
    },
    {
        "id": "billing.list_coupons", "label": "Coupons", "method": "GET",
        "path": "/api/v1/billing/coupons", "capability": "platform.billing", "group": "Catalogue",
        # There was no coupon read at all: an operator created a code and then had to remember it,
        # for ever, because nothing could tell them what existed.
        "params": [
            {"name": "status", "in": "query", "type": "enum", "required": False,
             "help": "active | scheduled | expired | exhausted | disabled"},
        ],
    },
    {
        "id": "billing.get_coupon", "label": "Coupon detail", "method": "GET",
        "path": "/api/v1/billing/coupons/{code}", "capability": "platform.billing",
        "group": "Catalogue",
        "params": [{"name": "code", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.update_coupon", "label": "Edit a coupon", "method": "PATCH",
        "path": "/api/v1/billing/coupons/{code}", "capability": "platform.billing",
        "group": "Catalogue", "confirm": True,
        "params": [
            {"name": "code", "in": "path", "type": "string", "required": True},
            {"name": "value", "in": "body", "type": "integer", "required": False},
            {"name": "type", "in": "body", "type": "enum", "required": False,
             "help": "percent | fixed | trial"},
            {"name": "starts_at", "in": "body", "type": "number", "required": False,
             "help": "epoch seconds; before this the coupon is scheduled, not active"},
            {"name": "expires_at", "in": "body", "type": "number", "required": False},
            {"name": "max_redemptions", "in": "body", "type": "integer", "required": False,
             "help": "0 = unlimited. Counts CUSTOMERS, not billing cycles"},
            {"name": "active", "in": "body", "type": "boolean", "required": False,
             "help": "false turns it off without deleting the redemption history"},
            {"name": "plan_id", "in": "body", "type": "string", "required": False,
             "help": "scope the code to ONE plan; empty = any plan"},
            {"name": "note", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "billing.delete_coupon", "label": "Delete a coupon", "method": "DELETE",
        "path": "/api/v1/billing/coupons/{code}", "capability": "platform.billing",
        "group": "Catalogue", "danger": True, "confirm": True,
        "params": [{"name": "code", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "billing.create_coupon", "label": "Create coupon", "method": "POST",
        "path": "/api/v1/billing/coupons", "capability": "platform.billing",
        "group": "Catalogue",
        "params": [
            {"name": "code", "in": "body", "type": "string", "required": True},
            {"name": "type", "in": "body", "type": "enum", "required": False,
             "help": "percent | fixed | trial"},
            {"name": "value", "in": "body", "type": "integer", "required": False},
            {"name": "max_redemptions", "in": "body", "type": "integer", "required": False,
             "help": "0 = unlimited. Counts CUSTOMERS, not billing cycles"},
            {"name": "currency", "in": "body", "type": "string", "required": False},
            {"name": "starts_at", "in": "body", "type": "number", "required": False},
            {"name": "expires_at", "in": "body", "type": "number", "required": False},
            {"name": "active", "in": "body", "type": "boolean", "required": False},
            {"name": "public", "in": "body", "type": "boolean", "required": False,
             "help": "show this campaign to customers on GET /billing/offers"},
            {"name": "plan_id", "in": "body", "type": "string", "required": False,
             "help": "scope the code to ONE plan; empty = any plan"},
            {"name": "note", "in": "body", "type": "string", "required": False},
        ],
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
    {
        # Recovery for a webhook that never arrived or failed verification: pull the CURRENT status
        # from the gateway and apply it. Idempotent — safe to run even if the webhook later shows up.
        "id": "billing.reconcile_payment", "label": "Reconcile a payment with the gateway",
        "method": "POST", "path": "/api/v1/billing/payments/{payment_id}/reconcile",
        "capability": "platform.billing", "group": "Payments", "confirm": True,
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
            {"name": "prices", "in": "body", "type": "array", "required": False,
             "help": "existing subscribers keep the price they were sold"},
            {"name": "limits", "in": "body", "type": "object", "required": False},
            {"name": "trial_days", "in": "body", "type": "integer", "required": False},
        ],
    },
    {
        "id": "billing.plan_availability", "label": "Retire or hide a plan", "method": "POST",
        "path": "/api/v1/billing/plans/{plan_id}/availability", "capability": "billing.manage",
        "group": "Catalogue", "confirm": True,
        "params": [
            {"name": "plan_id", "in": "path", "type": "string", "required": True},
            {"name": "active", "in": "body", "type": "boolean", "required": False,
             "help": "false: nobody new may subscribe. Existing subscribers are untouched"},
            {"name": "listed", "in": "body", "type": "boolean", "required": False,
             "help": "false: hidden from the plan list, still subscribable"},
        ],
    }
]
