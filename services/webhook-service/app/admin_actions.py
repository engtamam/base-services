"""webhook-service — admin actions (the complete operator surface).

Every user-tier route is operator-scoped (platform.webhooks / webhooks.manage for mutations, plus
webhooks.read for the views); there is no `_SELF` route. Actual delivery is internal-token
(/internal/webhooks/*) and inbound receipt is public-signed (/inbound/{source}), so neither is a
panel action. What the operator drives: register / edit / delete endpoints, rotate a signing secret,
inspect health, deliveries and the dead-letter queue, replay a delivery or a whole event, dispatch a
test event, and sweep retries. Rotating a secret and deleting an endpoint are danger + confirm.
Since Phase 44 the operator also owns the INBOUND source secrets and the runtime policy knobs, both
declared at the end. Those are distinct from an ENDPOINT's signing secret, which this service
generates and returns once to its owner — that is the customer's credential, not the operator's.
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
                {"name": "event_types", "in": "body", "type": "array", "required": False,
                 "help": "event types to deliver — the API field is `event_types`; empty means all"},
                {"name": "description", "in": "body", "type": "string", "required": False}]},
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
     "confirm": True,
     "params": [{"name": "tenant", "in": "body", "type": "string", "required": True},
                {"name": "type", "in": "body", "type": "string", "required": True,
                 "help": "event type; only endpoints subscribed to it receive the delivery"},
                {"name": "payload", "in": "body", "type": "object", "required": False}]},
    {"id": "webhooks.sweep", "label": "Sweep pending retries", "method": "POST",
     "path": "/api/v1/webhooks/sweep", "capability": "platform.webhooks", "group": "Deliveries",
     "confirm": True},
    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Inbound source secrets", "method": "GET",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate an inbound source secret", "method": "PUT",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. INBOUND_SECRET:stripe"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove an inbound source secret", "method": "DELETE",
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
             "help": "e.g. INBOUND_TIMESTAMP_TOLERANCE_SECONDS or RETRY_MAX_ATTEMPTS"},
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
        "id": "webhooks.list_events", "label": "Events", "method": "GET",
        "path": "/api/v1/webhooks/events", "capability": "webhooks.read", "group": "Events",
        # Events were reachable only by an id somebody already held, so replaying a failed
        # delivery required knowing the event id in advance — exactly what an operator
        # investigating a failure does not have.
        "params": [
            {"name": "type", "in": "query", "type": "string", "required": False},
            {"name": "limit", "in": "query", "type": "integer", "required": False},
            {"name": "cursor", "in": "query", "type": "string", "required": False},
        ],
    }
]
