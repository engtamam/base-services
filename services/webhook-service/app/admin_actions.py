"""webhook-service — admin actions (the complete operator surface).

Every user-tier route is operator-scoped (platform.webhooks / webhooks.manage for mutations, plus
webhooks.read for the views); there is no `_SELF` route. Actual delivery is internal-token
(/internal/webhooks/*) and inbound receipt is public-signed (/inbound/{source}), so neither is a
panel action. What the operator drives: register / edit / delete endpoints, rotate a signing secret,
inspect health, deliveries and the dead-letter queue, replay a delivery or a whole event, dispatch a
test event, and sweep retries. Rotating a secret and deleting an endpoint are danger + confirm.
Every action re-checks its capability at the route; this file only decides what is shown.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Endpoints ────────────────────────────────────────────────────────────
    {"id": "webhooks.list_endpoints", "label": "List endpoints", "method": "GET",
     "path": "/api/v1/webhooks/endpoints", "capability": "platform.webhooks", "group": "Endpoints"},
    {"id": "webhooks.register", "label": "Register an endpoint", "method": "POST",
     "path": "/api/v1/webhooks/endpoints", "capability": "platform.webhooks", "group": "Endpoints",
     "confirm": True,
     "params": [{"name": "url", "in": "body", "type": "string", "required": True},
                {"name": "events", "in": "body", "type": "object", "required": False}]},
    {"id": "webhooks.update_endpoint", "label": "Edit an endpoint", "method": "PATCH",
     "path": "/api/v1/webhooks/endpoints/{id}", "capability": "platform.webhooks", "group": "Endpoints",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.rotate_secret", "label": "Rotate signing secret", "method": "POST",
     "path": "/api/v1/webhooks/endpoints/{id}/rotate-secret", "capability": "platform.webhooks",
     "group": "Endpoints", "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True,
                 "help": "invalidates the old secret — receivers must be updated to the new one"}]},
    {"id": "webhooks.delete_endpoint", "label": "Delete an endpoint", "method": "DELETE",
     "path": "/api/v1/webhooks/endpoints/{id}", "capability": "platform.webhooks", "group": "Endpoints",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.endpoint_health", "label": "View endpoint health", "method": "GET",
     "path": "/api/v1/webhooks/endpoints/{id}/health", "capability": "platform.webhooks",
     "group": "Endpoints",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Deliveries ───────────────────────────────────────────────────────────
    {"id": "webhooks.deliveries", "label": "List deliveries", "method": "GET",
     "path": "/api/v1/webhooks/deliveries", "capability": "platform.webhooks", "group": "Deliveries"},
    {"id": "webhooks.dead_letters", "label": "List dead-letters", "method": "GET",
     "path": "/api/v1/webhooks/dead-letters", "capability": "platform.webhooks", "group": "Deliveries"},
    {"id": "webhooks.replay_delivery", "label": "Replay a delivery", "method": "POST",
     "path": "/api/v1/webhooks/deliveries/{id}/replay", "capability": "platform.webhooks",
     "group": "Deliveries", "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.replay_event", "label": "Replay a whole event", "method": "POST",
     "path": "/api/v1/webhooks/events/{id}/replay", "capability": "platform.webhooks",
     "group": "Deliveries", "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True,
                 "help": "re-delivers the event to EVERY subscribed endpoint"}]},
    {"id": "webhooks.dispatch", "label": "Dispatch a test event", "method": "POST",
     "path": "/api/v1/webhooks/dispatch", "capability": "platform.webhooks", "group": "Deliveries",
     "confirm": True},
    {"id": "webhooks.sweep", "label": "Sweep pending retries", "method": "POST",
     "path": "/api/v1/webhooks/sweep", "capability": "platform.webhooks", "group": "Deliveries",
     "confirm": True},
]
