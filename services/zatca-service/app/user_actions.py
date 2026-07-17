"""zatca-service — end-user (self-service) actions: a business's OWN KSA e-invoicing.

Managing your own ZATCA e-invoicing is a customer concern, tenant-scoped from the token. Every route
is guarded by require_any_capability("platform.zatca", "zatca.manage"), so the same routes serve a
platform operator (platform.zatca, admin_actions) and a business (here, via the per-tenant
zatca.manage cap): view settings, onboard to the ZATCA compliance and production environments, list
invoices, resubmit a rejected one, fetch its QR, and verify the invoice hash chain. Production
onboarding is danger + confirm — it moves the business onto the live tax authority. Invoice
finalization and the stuck-submission sweep are internal/operator and stay out of the user app. Each
route re-checks its capability and re-scopes to the caller's tenant on every call.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    {"id": "zatca.get_settings", "label": "E-invoicing settings", "method": "GET",
     "path": "/api/v1/zatca/settings", "capability": "zatca.manage", "group": "Settings"},
    {"id": "zatca.onboard_compliance", "label": "Onboard to compliance (CSID)", "method": "POST",
     "path": "/api/v1/zatca/onboard/compliance", "capability": "zatca.manage", "group": "Onboarding",
     "confirm": True,
     "params": [{"name": "otp", "in": "body", "type": "string", "required": True,
                 "help": "the one-time password from the ZATCA portal"}]},
    {"id": "zatca.onboard_production", "label": "Go live (production)", "method": "POST",
     "path": "/api/v1/zatca/onboard/production", "capability": "zatca.manage", "group": "Onboarding",
     "danger": True, "confirm": True},
    {"id": "zatca.list_invoices", "label": "My invoices", "method": "GET",
     "path": "/api/v1/zatca/invoices", "capability": "zatca.manage", "group": "Invoices"},
    {"id": "zatca.resubmit", "label": "Resubmit a rejected invoice", "method": "POST",
     "path": "/api/v1/zatca/invoices/{id}/resubmit", "capability": "zatca.manage", "group": "Invoices",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "zatca.invoice_qr", "label": "Invoice QR", "method": "GET",
     "path": "/api/v1/zatca/invoices/{id}/qr", "capability": "zatca.manage", "group": "Invoices",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "zatca.chain_verify", "label": "Verify my invoice chain", "method": "GET",
     "path": "/api/v1/zatca/chain/verify", "capability": "zatca.manage", "group": "Integrity"},
]
