"""notification-service — admin actions (message-template management).

The full user-capability operator surface is template management: an operator authors the templates
that every notification renders from. Deliberately absent:
  - a recipient's own inbox / read-receipts / preferences — those are `_SELF` (the person's own data),
    never an operator action.
  - /internal/notify and /internal/notifications/drain — internal-token, service-to-service routes,
    not user-capability panel actions (a drain is driven by the platform, not an operator click).
Every action re-checks notifications.templates at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "notifications.list_templates", "label": "List message templates", "method": "GET",
        "path": "/api/v1/templates", "capability": "notifications.templates", "group": "Templates",
    },
    {
        "id": "notifications.deliveries", "label": "Delivery log (did it send?)", "method": "GET",
        "path": "/api/v1/notifications/deliveries", "capability": "notifications.templates",
        "group": "Deliveries",
        # Recent delivery attempts across the platform — status, channel, provider, error. Recipients
        # are masked; the access is audited. The log was recorded but reachable from no route.
    },
    {
        "id": "notifications.upsert_template", "label": "Create / update a template", "method": "POST",
        "path": "/api/v1/templates", "capability": "notifications.templates", "group": "Templates",
        "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "template identifier, e.g. welcome_email"},
            {"name": "locale", "in": "body", "type": "string", "required": True},
            {"name": "subject", "in": "body", "type": "string", "required": True},
            {"name": "body", "in": "body", "type": "string", "required": True},
            {"name": "version", "in": "body", "type": "integer", "required": False},
        ],
    },
    # ── WhatsApp templates: added and submitted at RUNTIME from the panel, no redeploy ──
    {
        "id": "notifications.list_wa_templates", "label": "WhatsApp templates", "method": "GET",
        "path": "/api/v1/whatsapp/templates", "capability": "notifications.templates",
        "group": "WhatsApp",
        "params": [{"name": "status", "in": "query", "type": "string", "required": False,
                    "help": "pending | approved | rejected"}],
    },
    {
        "id": "notifications.register_wa_template", "label": "Add a WhatsApp template",
        "method": "POST", "path": "/api/v1/whatsapp/templates",
        "capability": "notifications.templates", "group": "WhatsApp", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True},
            {"name": "language", "in": "body", "type": "string", "required": True},
            {"name": "category", "in": "body", "type": "string", "required": True,
             "help": "UTILITY | MARKETING | AUTHENTICATION"},
            {"name": "body", "in": "body", "type": "string", "required": False,
             "help": "the template copy — you own it; the service stores it opaquely"},
            {"name": "variables", "in": "body", "type": "array", "required": False},
        ],
    },
    {
        "id": "notifications.submit_wa_template", "label": "Submit a template to Meta",
        "method": "POST", "path": "/api/v1/whatsapp/templates/{name}/{language}/submit",
        "capability": "notifications.templates", "group": "WhatsApp", "confirm": True,
        "params": [{"name": "name", "in": "path", "type": "string", "required": True},
                   {"name": "language", "in": "path", "type": "string", "required": True}],
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
]
