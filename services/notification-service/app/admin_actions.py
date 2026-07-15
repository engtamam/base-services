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
        "id": "notifications.upsert_template", "label": "Create / update a template", "method": "POST",
        "path": "/api/v1/templates", "capability": "notifications.templates", "group": "Templates",
        "confirm": True,
        "params": [
            {"name": "key", "in": "body", "type": "string", "required": True,
             "help": "template identifier, e.g. welcome_email"},
            {"name": "locale", "in": "body", "type": "string", "required": True},
            {"name": "subject", "in": "body", "type": "string", "required": True},
            {"name": "body", "in": "body", "type": "string", "required": True},
            {"name": "version", "in": "body", "type": "string", "required": False},
        ],
    },
]
