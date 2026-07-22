"""authz-service — admin actions. THE control plane (KD-33: the admin is the control plane).

Editing a role's capabilities changes what everyone holding it may do on their NEXT mint — the most
far-reaching action in the platform, so role/policy mutations are danger+confirm. Every action
re-checks platform.authz / authz.manage at the route; this file only decides what the panel shows.
Complete set of the `_ADMIN`-gated routes (create/read/update/delete for roles and policies, and
subject assignment)."""
from __future__ import annotations

from typing import Any


ADMIN_ACTIONS: list[dict[str, Any]] = [
    # ── Roles ────────────────────────────────────────────────────────────────
    {
        "id": "authz.list_roles", "label": "List roles", "method": "GET",
        "path": "/api/v1/roles", "capability": "platform.authz", "group": "Roles",
    },
    {
        "id": "authz.get_role", "label": "View a role", "method": "GET",
        "path": "/api/v1/roles/{role_id}", "capability": "platform.authz", "group": "Roles",
        "params": [{"name": "role_id", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "authz.create_role", "label": "Create role", "method": "POST",
        "path": "/api/v1/roles", "capability": "platform.authz", "group": "Roles", "confirm": True,
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True},
            {"name": "capabilities", "in": "body", "type": "array", "required": False,
             "help": "list of capability strings"},
        ],
    },
    {
        "id": "authz.edit_role", "label": "Edit role capabilities", "method": "PUT",
        "path": "/api/v1/roles/{role_id}", "capability": "platform.authz", "group": "Roles",
        "danger": True, "confirm": True,
        "params": [
            {"name": "role_id", "in": "path", "type": "string", "required": True},
            {"name": "capabilities", "in": "body", "type": "array", "required": False,
             "help": "changes what EVERYONE holding this role may do on their next mint"},
        ],
    },
    {
        "id": "authz.delete_role", "label": "Delete role", "method": "DELETE",
        "path": "/api/v1/roles/{role_id}", "capability": "platform.authz", "group": "Roles",
        "danger": True, "confirm": True,
        "params": [{"name": "role_id", "in": "path", "type": "string", "required": True}],
    },
    # ── Policies (ABAC) ──────────────────────────────────────────────────────
    {
        "id": "authz.list_policies", "label": "List policies", "method": "GET",
        "path": "/api/v1/policies", "capability": "platform.authz", "group": "Policies",
    },
    {
        "id": "authz.create_policy", "label": "Create policy", "method": "POST",
        "path": "/api/v1/policies", "capability": "platform.authz", "group": "Policies",
        "confirm": True,
        "params": [
            {"name": "effect", "in": "body", "type": "enum", "required": True,
             "enum": ["allow", "deny"]},
            {"name": "action", "in": "body", "type": "string", "required": False},
            {"name": "resource", "in": "body", "type": "string", "required": False},
            {"name": "condition", "in": "body", "type": "object", "required": False},
            {"name": "priority", "in": "body", "type": "integer", "required": False},
            {"name": "description", "in": "body", "type": "string", "required": False},
        ],
    },
    {
        "id": "authz.delete_policy", "label": "Delete policy", "method": "DELETE",
        "path": "/api/v1/policies/{policy_id}", "capability": "platform.authz", "group": "Policies",
        "danger": True, "confirm": True,
        "params": [{"name": "policy_id", "in": "path", "type": "string", "required": True}],
    },
    # ── Assignments ──────────────────────────────────────────────────────────
    {
        "id": "authz.get_subject_roles", "label": "View a subject's roles", "method": "GET",
        "path": "/api/v1/subjects/{subject}/roles", "capability": "platform.authz",
        "group": "Assignments",
        "params": [{"name": "subject", "in": "path", "type": "string", "required": True}],
    },
    {
        "id": "authz.assign_roles", "label": "Assign roles to a subject", "method": "PUT",
        "path": "/api/v1/subjects/{subject}/roles", "capability": "platform.authz",
        "group": "Assignments", "confirm": True,
        "params": [
            {"name": "subject", "in": "path", "type": "string", "required": True},
            {"name": "roles", "in": "body", "type": "array", "required": False,
             "help": "role ids; replaces the subject's current set"},
            {"name": "capabilities", "in": "body", "type": "array", "required": False,
             "help": "direct capability grants, alongside the roles"},
        ],
    },
]
