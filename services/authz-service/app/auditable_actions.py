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
]
