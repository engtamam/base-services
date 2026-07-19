"""webhook-service — auditable actions.

The endpoint secret is what proves a delivery came from us, so rotating it is critical: an
unattributable rotation lets someone redirect trust. Registering an endpoint is critical for the
same reason a connector is — it is an outbound data path to a URL someone chose. Delivery and
dispatch are volume work.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "webhook.endpoint_updated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "webhook.dispatched",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "webhook.delivered",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "webhook.swept",
        "source": "system",
        "critical": False,
    },
    {
        "action": "webhook.replayed",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "webhook.endpoint_created",
        "source": "admin",
        "critical": True,
        "why": "an endpoint is an outbound data path to a URL someone chose; an unattributable "
               "one sends this platform's events to a destination nobody reviewed",
    },
    {
        "action": "webhook.endpoint_deleted",
        "source": "admin",
        "critical": True,
        "why": "deleting an endpoint silently stops a customer's integration receiving events",
    },
    {
        "action": "webhook.secret_rotated",
        "source": "admin",
        "critical": True,
        "why": "the secret is what proves a delivery came from us; rotating it lets someone "
               "redirect that trust, and the customer's verification breaks until they are "
               "told",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
