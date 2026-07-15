"""zatca-service — admin actions (the complete operator surface).

ZATCA e-invoicing (KSA). Invoice finalization and the scheduled sweep run on internal-token routes
(/internal/zatca/*); the panel drives the operator side. Every user-tier route is _ADMIN
(platform.zatca / zatca.manage): view and edit settings, onboard to the ZATCA compliance and
production environments, list invoices, resubmit a rejected one, fetch its QR, verify the invoice
hash chain, and sweep stuck submissions. Production onboarding is danger + confirm — it moves the
tenant onto the live tax authority. Every action re-checks its capability at the route; this file
only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Settings ─────────────────────────────────────────────────────────────
    {"id": "zatca.get_settings", "label": "View settings", "method": "GET",
     "path": "/api/v1/zatca/settings", "capability": "platform.zatca", "group": "Settings"},
    {"id": "zatca.put_settings", "label": "Edit settings", "method": "PUT",
     "path": "/api/v1/zatca/settings", "capability": "platform.zatca", "group": "Settings",
     "confirm": True},
    # ── Onboarding ───────────────────────────────────────────────────────────
    {"id": "zatca.onboard_compliance", "label": "Onboard to compliance (CSID)", "method": "POST",
     "path": "/api/v1/zatca/onboard/compliance", "capability": "platform.zatca", "group": "Onboarding",
     "confirm": True,
     "params": [{"name": "otp", "in": "body", "type": "string", "required": True,
                 "help": "the one-time password from the ZATCA portal"}]},
    {"id": "zatca.onboard_production", "label": "Onboard to production (live tax authority)",
     "method": "POST", "path": "/api/v1/zatca/onboard/production", "capability": "platform.zatca",
     "group": "Onboarding", "danger": True, "confirm": True},
    # ── Invoices ─────────────────────────────────────────────────────────────
    {"id": "zatca.list_invoices", "label": "List invoices", "method": "GET",
     "path": "/api/v1/zatca/invoices", "capability": "platform.zatca", "group": "Invoices"},
    {"id": "zatca.resubmit", "label": "Resubmit a rejected invoice", "method": "POST",
     "path": "/api/v1/zatca/invoices/{id}/resubmit", "capability": "platform.zatca", "group": "Invoices",
     "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "zatca.invoice_qr", "label": "View invoice QR", "method": "GET",
     "path": "/api/v1/zatca/invoices/{id}/qr", "capability": "platform.zatca", "group": "Invoices",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Integrity ────────────────────────────────────────────────────────────
    {"id": "zatca.chain_verify", "label": "Verify invoice hash chain", "method": "GET",
     "path": "/api/v1/zatca/chain/verify", "capability": "platform.zatca", "group": "Integrity"},
    {"id": "zatca.sweep", "label": "Sweep stuck submissions", "method": "POST",
     "path": "/api/v1/zatca/sweep", "capability": "platform.zatca", "group": "Integrity",
     "confirm": True},
]
