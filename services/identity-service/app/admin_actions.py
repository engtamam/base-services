"""identity-service — admin actions (the multi-tenant member directory, Phase 31).

Only what an operator drives with a USER token + capability appears here. Deliberately absent:
  - a person's password / MFA reset — a tenant admin may NEVER touch a person's credentials (T2);
    credential recovery belongs to the person alone. There is no such route to declare.
  - /internal/ratelimit/unblock and /internal/tenancy/members — internal-token routes, not
    user-capability routes, so they are service-to-service, not panel actions.

Caps default to members:read / members:write (overridable via IDENTITY_MEMBERS_*_CAP). The routes
exist only when IDENTITY_MULTI_TENANT is on; off, they 404 and the panel simply gets a stale entry
(harmless — the truth is the service, never the catalog). Every action re-checks its cap at the
route."""
from __future__ import annotations

from typing import Any

ADMIN_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "identity.list_members",
        "label": "List members",
        "method": "GET",
        "path": "/api/v1/tenancy/members",
        "capability": "members:read",
        "group": "Directory",
    },
    {
        "id": "identity.add_member",
        "label": "Add member (invite)",
        "method": "POST",
        "path": "/api/v1/tenancy/members",
        "capability": "members:write",
        "group": "Directory",
        "confirm": True,
        "params": [
            {"name": "identifier", "in": "body", "type": "string", "required": True,
             "help": "the person's login identifier — an invite is sent; the admin never sets the password"},
            {"name": "roles", "in": "body", "type": "object", "required": False},
        ],
    },
    {
        "id": "identity.remove_member",
        "label": "Remove member (revoke this tenant only)",
        "method": "DELETE",
        "path": "/api/v1/tenancy/members/{person_id}",
        "capability": "members:write",
        "group": "Directory",
        "danger": True,
        "confirm": True,
        "params": [{"name": "person_id", "in": "path", "type": "string", "required": True,
                    "help": "revokes this membership only — the person survives, they work elsewhere"}],
    },
]
