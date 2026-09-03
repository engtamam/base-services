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
     "path": "/api/v1/zatca/invoices", "capability": "platform.zatca", "group": "Invoices",
     "params": [
         {"name": "status", "in": "query", "type": "string", "required": False,
          "help": "pending | signed | submitted | cleared | reported | rejected | failed"},
         {"name": "needs_attention", "in": "query", "type": "boolean", "required": False,
          "help": "anything not yet cleared by the tax authority"},
         {"name": "limit", "in": "query", "type": "integer", "required": False},
         {"name": "cursor", "in": "query", "type": "string", "required": False},
     ]},
    {"id": "zatca.invoice_detail", "label": "View an invoice's compliance detail", "method": "GET",
     "path": "/api/v1/zatca/invoices/{id}", "capability": "platform.zatca", "group": "Invoices",
     # Status, warnings, ZATCA errors, retry state — the see-before-you-resubmit read.
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
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
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Provider credentials", "method": "GET",
        "path": "/api/v1/zatca/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a provider credential", "method": "PUT",
        "path": "/api/v1/zatca/operator/secrets", "capability": "platform.secrets",
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
        "path": "/api/v1/zatca/operator/secrets/{name}", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "operator.get_config", "label": "Runtime settings", "method": "GET",
        "path": "/api/v1/zatca/operator/config", "capability": "platform.config", "group": "Settings",
    },
    {
        "id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
        "path": "/api/v1/zatca/operator/config", "capability": "platform.config",
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
        "path": "/api/v1/zatca/operator/config/{key}", "capability": "platform.config",
        "group": "Settings", "confirm": True,
        "params": [{"name": "key", "in": "path", "type": "string", "required": True}],
    }
]
