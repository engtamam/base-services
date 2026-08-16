"""tenant-service — auditable actions.

Tenants are customers. Suspending one takes an entire organisation off the platform, and deleting
one removes it, so both are critical and both will be disputed. Config and branding changes are
routine.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "tenant.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "tenant.renamed",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "tenant.config_updated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "tenant.branding_updated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "tenant.suspended",
        "source": "admin",
        "critical": True,
        "why": "suspension takes an entire customer organisation off the platform; they will "
               "dispute it and we must be able to say who did it and when",
    },
    {
        "action": "tenant.reactivated",
        "source": "admin",
        "critical": True,
        "why": "reactivating restores access for a whole organisation, including one suspended "
               "for abuse",
    },
    {
        "action": "tenant.deleted",
        "source": "admin",
        "critical": True,
        "why": "deleting a tenant removes a customer and their data; nothing else can show who "
               "authorised it",
    },
    {
        "action": "tenant.close_requested",
        "source": "user",
        "critical": True,
        "why": "an owner closing their own organisation begins its erasure after a grace window; it "
               "is the most consequential self-service action there is, and a disputed or "
               "unauthorised close must be attributable to whoever asked for it",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
