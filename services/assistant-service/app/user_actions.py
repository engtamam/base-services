"""<service> — end-user (self-service) actions for the platform app catalog (Phase 36).

The end-user app (GET /api/v1/app/catalog) aggregates what every service declares HERE — the twin of
`admin_actions`, but for a USER acting on their OWN data (their files, their profile, their
subscription), never platform-operator routes. The code is the source, so the app can never drift.

Declaring nothing is fine — keep the list empty and reviewed rather than forgotten. Each action is a
PURE literal dict (the safety:user-actions CI gate reads it with ast.literal_eval — no variables, no
f-strings). Shape (docs/contracts/user-actions.schema.json):

    {
        "id": "svc.do_thing",              # stable, kebab/dot, unique
        "label": "Do the thing",           # what the control says
        "method": "POST",                  # GET|POST|PUT|PATCH|DELETE (allow-listed)
        "path": "/api/v1/things/{id}",     # RELATIVE — no scheme/host; must be a real manifest route
        "capability": "things.write",      # the cap the route ENFORCES; must be advertised in the manifest
        "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
        "confirm": True,                   # UX: ask before firing (auto-true when danger)
        "danger": True,                    # UX: destructive for the user (e.g. delete my account)
        "group": "Things",                 # UX: section
    }

Rules the gate enforces: the path must exist in this service's manifest `endpoints`; the capability
must be in the manifest's advertised `capabilities`. The app is a MAP, never the AUTHORITY — every
action is re-checked AND re-scoped to the caller's own tenant/identity at its route. Only declare the
user's OWN self-service (`_SELF` / ordinary user-cap) routes; operator (platform.*) and internal-token
routes belong in admin_actions or nowhere.
"""
from __future__ import annotations

from typing import Any


USER_ACTIONS: list[dict[str, Any]] = [
    # Both are ordinary self-service: asking for help is not a privileged act. `assistant.ask`
    # gates the ROUTE; what the assistant will actually answer is gated per entry by its own
    # `requires`, so holding this capability grants access to the assistant and to nothing else.
    {"id": "assistant.ask", "label": "Ask the assistant", "method": "POST",
     "path": "/api/v1/assistant/ask", "capability": "assistant.ask", "group": "Help",
     "params": [{"name": "question", "in": "body", "type": "string", "required": True},
                {"name": "surface", "in": "body", "type": "string", "required": False}]},
    {"id": "assistant.help", "label": "Help for this control", "method": "GET",
     "path": "/api/v1/assistant/help", "capability": "assistant.ask", "group": "Help",
     "params": [{"name": "anchor", "in": "query", "type": "string", "required": True}]},
    {"id": "assistant.my_conversations", "label": "My past questions", "method": "GET",
     "path": "/api/v1/assistant/conversations", "capability": "assistant.ask", "group": "Help"},
    {"id": "assistant.read_conversation", "label": "Read a past conversation", "method": "GET",
     "path": "/api/v1/assistant/conversations/{id}", "capability": "assistant.ask", "group": "Help",
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
    {"id": "assistant.delete_conversation", "label": "Delete a conversation", "method": "DELETE",
     "path": "/api/v1/assistant/conversations/{id}", "capability": "assistant.ask", "group": "Help",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]


UNDECLARED_ROUTES: list[dict[str, str]] = [
    {"method": "GET", "path": "/api/v1/assistant/branding",
     "why": "the widget reads its own colours and logo here — it is presentation the client fetches for itself, not a control a panel renders as a button"},
    {"method": "GET", "path": "/api/v1/whoami",
     "why": "scaffold diagnostic from the template: echoes the caller's own claims, so it is a debugging aid rather than a control a panel renders"},
    {"method": "GET", "path": "/api/v1/admin/ping",
     "why": "scaffold liveness probe from the template — proves the admin tier authorises, carries no data and does nothing"},
]
