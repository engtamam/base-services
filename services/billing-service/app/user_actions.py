"""billing-service — end-user (self-service) actions: a customer's OWN billing.

The `_SELF` routes (billing.self / billing.manage / platform.billing): browse plans, subscribe,
change or cancel my plan, view my subscriptions, invoices (and their PDFs), and my usage. Every one is
scoped to the caller's tenant/subscription from the token — a customer only ever sees their own money.
Operator-only surfaces (create plans/coupons, refund, settle a bank transfer, run dunning, migrate
terms, the ledger) enforce platform.billing / billing.manage and live in admin_actions, not here.
`billing.self` is the advertised self-service capability. The app is a map — each route re-checks its
capability and re-scopes to the caller on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "billing.plans", "label": "Browse plans", "method": "GET",
     "path": "/api/v1/billing/plans", "capability": "billing.self", "group": "Plan"},
    {"id": "billing.my_subscriptions", "label": "My subscription", "method": "GET",
     "path": "/api/v1/billing/subscriptions", "capability": "billing.self", "group": "Plan"},
    {"id": "billing.subscribe", "label": "Subscribe to a plan", "method": "POST",
     "path": "/api/v1/billing/subscriptions", "capability": "billing.self", "group": "Plan",
     "confirm": True,
     "params": [{"name": "plan_id", "in": "body", "type": "string", "required": True}]},
    {"id": "billing.change_plan", "label": "Change my plan", "method": "POST",
     "path": "/api/v1/billing/subscriptions/{id}/change", "capability": "billing.self", "group": "Plan",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True},
                {"name": "plan_id", "in": "body", "type": "string", "required": True}]},
    {"id": "billing.pause", "label": "Pause my plan", "method": "POST",
     "path": "/api/v1/billing/subscriptions/{id}/pause", "capability": "billing.self", "group": "Plan",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.reactivate", "label": "Resume my plan", "method": "POST",
     "path": "/api/v1/billing/subscriptions/{id}/reactivate", "capability": "billing.self", "group": "Plan",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.cancel", "label": "Cancel my plan", "method": "POST",
     "path": "/api/v1/billing/subscriptions/{id}/cancel", "capability": "billing.self", "group": "Plan",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.start_checkout", "label": "Pay now", "method": "POST",
     "path": "/api/v1/billing/checkouts", "capability": "billing.self", "group": "Plan",
     "confirm": True,
     "params": [{"name": "amount_minor", "in": "body", "type": "integer", "required": True}]},
    {"id": "billing.my_invoices", "label": "My invoices", "method": "GET",
     "path": "/api/v1/billing/invoices", "capability": "billing.self", "group": "Invoices"},
    {"id": "billing.invoice_pdf", "label": "Download the tax invoice", "method": "GET",
     "path": "/api/v1/billing/invoices/{id}/pdf", "capability": "billing.self", "group": "Invoices",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.invoice_receipt", "label": "Download the payment receipt", "method": "GET",
     "path": "/api/v1/billing/invoices/{id}/receipt", "capability": "billing.self", "group": "Invoices",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.invoice_document", "label": "Download the document", "method": "GET",
     "path": "/api/v1/billing/invoices/{id}/document", "capability": "billing.self", "group": "Invoices",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.invoice_settlements", "label": "Payments against an invoice", "method": "GET",
     "path": "/api/v1/billing/invoices/{invoice_id}/settlements", "capability": "billing.self",
     "group": "Invoices",
     "params": [{"name": "invoice_id", "in": "path", "type": "string", "required": True}]},
    {"id": "billing.my_usage", "label": "My usage", "method": "GET",
     "path": "/api/v1/billing/usage", "capability": "billing.self", "group": "Usage"},
]

# Routes deliberately NOT rendered as controls. Machine-checked by safety:action-coverage: every
# user-tier route must be declared above or excluded here WITH a reason.
UNDECLARED_ROUTES: list[dict[str, str]] = [
    {"method": "POST", "path": "/api/v1/billing/usage",
     "why": "metering ingestion a client SDK calls per event — a firehose, not a button"},
    {"method": "GET", "path": "/api/v1/billing/entitlements/{key}",
     "why": "a fail-closed permission check other services make programmatically before acting"},
]
