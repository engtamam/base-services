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
            {"name": "roles", "in": "body", "type": "array", "required": False},
        ],
    },
    {
        "id": "identity.update_member",
        "label": "Change a member's roles",
        "method": "PUT",
        "path": "/api/v1/tenancy/members/{person_id}",
        "capability": "members:write",
        "group": "Directory",
        "confirm": True,
        "params": [
            {"name": "person_id", "in": "path", "type": "string", "required": True},
            {"name": "roles", "in": "body", "type": "array", "required": False,
             "help": "the roles the member holds in THIS tenant — never touches their credential"},
            {"name": "caps", "in": "body", "type": "array", "required": False},
        ],
    },
    {
        "id": "identity.set_member_status",
        "label": "Suspend or reactivate a member",
        "method": "POST",
        "path": "/api/v1/tenancy/members/{person_id}/status",
        "capability": "members:write",
        "group": "Directory",
        "confirm": True,
        "params": [
            {"name": "person_id", "in": "path", "type": "string", "required": True},
            {"name": "status", "in": "body", "type": "string", "required": True,
             "help": "active | suspended — suspend revokes access without deleting the member"},
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

    # ── Operator controls: credentials and policy, changeable after launch ────
    # A panel that can only ACT is half a panel: the reads are declared too, so an operator can
    # see which credential is loaded before replacing it. No action here returns a value — the
    # list shows a four-character hint, and safety:no-secret-reads fails the build if that
    # ever changes.
    {
        "id": "operator.list_secrets", "label": "Service credentials", "method": "GET",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials",
    },
    {
        "id": "operator.set_secret", "label": "Set / rotate a credential", "method": "PUT",
        "path": "/api/v1/operator/secrets", "capability": "platform.secrets",
        "group": "Credentials", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "help": "the setting it replaces, e.g. AUTHZ_INTERNAL_TOKEN. Token SIGNING material "
                     "is not here: changing it under live sessions logs out the whole platform"},
            {"name": "value", "in": "body", "type": "string", "required": True,
             "help": "write-only: it is sealed on arrival and no route can read it back"},
            {"name": "tenant", "in": "body", "type": "string", "required": False,
             "help": "leave empty for platform-wide"},
        ],
    },
    {
        "id": "operator.clear_secret", "label": "Remove a credential", "method": "DELETE",
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
             "help": "e.g. IDENTITY_INVITE_TTL_SECONDS"},
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
