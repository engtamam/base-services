"""notification-service — auditable actions.

Sending is the volume path and is never blocked — refusing to send because the audit log is down
would turn a logging problem into undelivered password resets. Preference changes are critical for
a narrower reason: they control whether a person can be contacted at all, and silently switching
someone's notifications off is a way to hide activity from them.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "notification.sent",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "notification.read",
        "source": "user",
        "critical": False,
    },
    {
        "action": "notification.drained",
        "source": "system",
        "critical": False,
    },
    {
        "action": "preferences.updated",
        "source": "user",
        "critical": True,
        "why": "preferences decide whether a person can be contacted; silently disabling "
               "someone's alerts is how account activity gets hidden from its owner",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
