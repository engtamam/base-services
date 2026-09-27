"""authz-service — auditable actions.

This service IS the permission system, so almost everything it does is a grant of power and almost
everything is critical. The one deliberate exception is the check endpoint: it is the hottest path
in the platform, called before every authorising decision, and auditing each check would record
millions of routine allows to find the occasional deny.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "role.created",
        "source": "admin",
        "critical": True,
        "why": "a role is a bundle of power; an unattributable new role is indistinguishable "
               "from an attacker minting themselves privileges",
    },
    {
        "action": "role.updated",
        "source": "admin",
        "critical": True,
        "why": "widening a role silently grants power to everyone who already holds it",
    },
    {
        "action": "role.deleted",
        "source": "admin",
        "critical": True,
        "why": "deleting a role revokes access from everyone holding it, and they can dispute "
               "it",
    },
    {
        "action": "policy.created",
        "source": "admin",
        "critical": True,
        "why": "a policy decides who may do what; an unrecorded policy change cannot be "
               "reviewed",
    },
    {
        "action": "policy.deleted",
        "source": "admin",
        "critical": True,
        "why": "removing a policy can silently widen access rather than narrow it",
    },
    {
        "action": "subject.roles_assigned",
        "source": "admin",
        "critical": True,
        "why": "this is the permission grant itself — the single most important line in any "
               "access investigation, and an unattributable one looks exactly like an "
               "attacker's",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
    {
        "action": "authz.tenant_bootstrapped",
        "source": "internal",
        "critical": True,
        "why": "this is the ONE act that mints a tenant's first admin (authz.manage) and grants it "
               "to the owner — the seed of every privilege in that company. Who created which roles "
               "and made whom an admin, and when, is the record a security review starts from, and "
               "it is unavailable anywhere else once the saga has run",
    },
    {
        "action": "authz.backup_imported",
        "source": "internal",
        "critical": True,
        "why": "a backup import can overwrite or prune the ENTIRE authz state (roles, policies, "
               "assignments) — the most privilege-relevant mutation this service has. Without a record "
               "a state rewrite (accidental or malicious, via a leaked internal token) leaves no trail, "
               "unlike every individual role/policy/assignment write which is audited",
    },
]
