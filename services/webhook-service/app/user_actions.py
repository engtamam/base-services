"""webhook-service — end-user (self-service) actions: a customer's OWN webhooks.

Webhook endpoints are a customer/developer concern — you register YOUR endpoints and watch YOUR
deliveries, all tenant-scoped from the token. The routes are gated by require_any_capability that
accepts the per-tenant developer caps `webhooks.manage` (writes) / `webhooks.read` (reads), so the
same routes serve a platform operator (platform.webhooks, admin_actions) and a customer (here).
Declared: manage my endpoints (register / edit / rotate secret / delete / health), watch deliveries
and the dead-letter queue, replay a delivery, and send a test event. The purely operational sweep and
whole-event replay stay operator-only. Each route re-checks its capability and re-scopes to the caller.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # ── Endpoints ────────────────────────────────────────────────────────────
    {"id": "webhooks.list_endpoints", "label": "My endpoints", "method": "GET",
     "path": "/api/v1/webhooks/endpoints", "capability": "webhooks.read", "group": "Endpoints"},
    {"id": "webhooks.register", "label": "Add an endpoint", "method": "POST",
     "path": "/api/v1/webhooks/endpoints", "capability": "webhooks.manage", "group": "Endpoints",
     "confirm": True,
     "params": [{"name": "url", "in": "body", "type": "string", "required": True}]},
    {"id": "webhooks.update_endpoint", "label": "Edit an endpoint", "method": "PATCH",
     "path": "/api/v1/webhooks/endpoints/{id}", "capability": "webhooks.manage", "group": "Endpoints",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.rotate_secret", "label": "Rotate signing secret", "method": "POST",
     "path": "/api/v1/webhooks/endpoints/{id}/rotate-secret", "capability": "webhooks.manage",
     "group": "Endpoints", "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.delete_endpoint", "label": "Delete an endpoint", "method": "DELETE",
     "path": "/api/v1/webhooks/endpoints/{id}", "capability": "webhooks.manage", "group": "Endpoints",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.endpoint_health", "label": "Endpoint health", "method": "GET",
     "path": "/api/v1/webhooks/endpoints/{id}/health", "capability": "webhooks.read",
     "group": "Endpoints",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    # ── Deliveries ───────────────────────────────────────────────────────────
    {"id": "webhooks.deliveries", "label": "My deliveries", "method": "GET",
     "path": "/api/v1/webhooks/deliveries", "capability": "webhooks.read", "group": "Deliveries"},
    {"id": "webhooks.dead_letters", "label": "Failed deliveries", "method": "GET",
     "path": "/api/v1/webhooks/dead-letters", "capability": "webhooks.read", "group": "Deliveries"},
    {"id": "webhooks.replay_delivery", "label": "Replay a delivery", "method": "POST",
     "path": "/api/v1/webhooks/deliveries/{id}/replay", "capability": "webhooks.manage",
     "group": "Deliveries", "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "webhooks.dispatch", "label": "Send a test event", "method": "POST",
     "path": "/api/v1/webhooks/dispatch", "capability": "webhooks.manage", "group": "Deliveries",
     "confirm": True},
]
