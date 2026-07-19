"""job-service — auditable actions.

The queue mechanics — claim, ack, nack, heartbeat — run thousands of times a minute and are
deliberately not audited: recording them would bury the log in noise to capture nothing anyone
investigates. What IS audited is a human reaching in: replaying a job re-executes real work, and
cancelling stops work someone was relying on.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "schedule.created",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "schedule.deleted",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "job.enqueued",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "job.replayed",
        "source": "admin",
        "critical": True,
        "why": "replaying re-executes real side effects — a second charge, a second email — "
               "and an unattributable replay is indistinguishable from a duplicate someone "
               "caused deliberately",
    },
    {
        "action": "job.cancelled",
        "source": "admin",
        "critical": True,
        "why": "cancelling stops work a customer is waiting on; without a record nobody can "
               "say why it never ran",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
