"""<service> — admin actions declared for the platform admin panel (Phase 33).

The panel aggregates what every service declares HERE (it is served on /internal/manifest, so the
code is the source and the panel can never drift). Declaring nothing is fine — a service with an
empty list simply exposes no controls; keep it empty and reviewed rather than forgotten.

Each action is a PURE literal dict (the safety:admin CI gate reads it with ast.literal_eval — no
variables, no f-strings). Shape (docs/contracts/admin-actions.schema.json):

    {
        "id": "svc.do_thing",              # stable, kebab/dot, unique
        "label": "Do the thing",           # what the button says
        "method": "POST",                  # GET|POST|PUT|PATCH|DELETE (allow-listed)
        "path": "/api/v1/things/{id}/do",  # RELATIVE — no scheme/host; must be a real manifest route
        "capability": "platform.svc",      # the cap the route ENFORCES; must be advertised in the manifest
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
        "confirm": True,                   # UX: ask before firing (auto-true when danger)
        "danger": True,                    # UX: destructive/irreversible
        "group": "Things",                 # UX: section
    }

Rules the gate enforces: the path must exist in this service's manifest `endpoints`; the capability
must be in the manifest's advertised `capabilities`; the panel is a MAP, never the AUTHORITY — every
action is re-checked at its own route on every call. Only declare OPERATOR (platform.* / *.manage /
*.read) routes; internal-token and per-user `_SELF` routes are never panel actions.
"""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    {"id": "tickets.inbox", "label": "All tickets", "method": "GET",
     "path": "/api/v1/operator/tickets", "capability": "tickets.manage", "group": "Tickets",
     "params": [{"name": "status", "in": "query", "type": "string", "required": False},
                {"name": "type", "in": "query", "type": "string", "required": False},
                {"name": "assignee", "in": "query", "type": "string", "required": False}]},
    {"id": "tickets.transition", "label": "Change ticket status", "method": "POST",
     "path": "/api/v1/tickets/{ticket_id}/status", "capability": "tickets.manage", "group": "Tickets",
     "confirm": True,
     "params": [{"name": "ticket_id", "in": "path", "type": "string", "required": True},
                {"name": "to", "in": "body", "type": "string", "required": True,
                 "help": "must be an allowed next status for the ticket's type"}]},
    {"id": "tickets.assign", "label": "Assign a ticket", "method": "POST",
     "path": "/api/v1/tickets/{ticket_id}/assign", "capability": "tickets.manage", "group": "Tickets",
     "params": [{"name": "ticket_id", "in": "path", "type": "string", "required": True},
                {"name": "assignee", "in": "body", "type": "string", "required": True}]},
    {"id": "tickets.escalate", "label": "Escalate a ticket", "method": "POST",
     "path": "/api/v1/tickets/{ticket_id}/escalate", "capability": "tickets.manage", "group": "Tickets",
     "params": [{"name": "ticket_id", "in": "path", "type": "string", "required": True},
                {"name": "level", "in": "body", "type": "string", "required": False,
                 "help": "an optional severity/tier label recorded with the escalation"},
                {"name": "reason", "in": "body", "type": "string", "required": False}]},
    {"id": "tickets.tag", "label": "Tag a ticket", "method": "POST",
     "path": "/api/v1/tickets/{ticket_id}/tags", "capability": "tickets.manage", "group": "Tickets",
     "params": [{"name": "ticket_id", "in": "path", "type": "string", "required": True},
                {"name": "tags", "in": "body", "type": "array", "required": True}]},
    {"id": "tickets.suggest_reply", "label": "Draft a reply with AI", "method": "POST",
     "path": "/api/v1/tickets/{ticket_id}/suggest-reply", "capability": "tickets.manage",
     "group": "Tickets",
     "params": [{"name": "ticket_id", "in": "path", "type": "string", "required": True}]},

    # ── operator: credentials and runtime settings (the template's operator surface) ──
    {"id": "operator.list_secrets", "label": "Service credentials", "method": "GET",
     "path": "/api/v1/operator/secrets", "capability": "platform.secrets", "group": "Credentials"},
    {"id": "operator.set_secret", "label": "Set / rotate a credential", "method": "PUT",
     "path": "/api/v1/operator/secrets", "capability": "platform.secrets", "group": "Credentials",
     "confirm": True,
     "params": [{"name": "name", "in": "body", "type": "string", "required": True},
                {"name": "value", "in": "body", "type": "string", "required": True,
                 "help": "write-only: sealed on arrival and no route can read it back"},
                {"name": "tenant", "in": "body", "type": "string", "required": False}]},
    {"id": "operator.clear_secret", "label": "Remove a credential", "method": "DELETE",
     "path": "/api/v1/operator/secrets/{name}", "capability": "platform.secrets",
     "group": "Credentials", "confirm": True,
     "params": [{"name": "name", "in": "path", "type": "string", "required": True}]},
    {"id": "operator.get_config", "label": "Runtime settings", "method": "GET",
     "path": "/api/v1/operator/config", "capability": "platform.config", "group": "Settings"},
    {"id": "operator.set_config", "label": "Change a runtime setting", "method": "PUT",
     "path": "/api/v1/operator/config", "capability": "platform.config", "group": "Settings",
     "confirm": True,
     "params": [{"name": "key", "in": "body", "type": "string", "required": True},
                {"name": "value", "in": "body", "type": "string", "required": True},
                {"name": "tenant", "in": "body", "type": "string", "required": False}]},
    {"id": "operator.clear_config", "label": "Reset a setting to its default", "method": "DELETE",
     "path": "/api/v1/operator/config/{key}", "capability": "platform.config", "group": "Settings",
     "confirm": True,
     "params": [{"name": "key", "in": "path", "type": "string", "required": True}]},
]
