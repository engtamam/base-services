"""search-service — auditable actions.

The index is a copy of other services' data, so deletion is the action that matters: a bulk delete
removes a customer's searchable record and cannot be undone from here. Indexing is volume work and
never blocks.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "search.indexed",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "search.bulk_indexed",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "search.deleted",
        "source": "internal",
        "critical": True,
        "why": "deleting from the index removes a customer's searchable data irreversibly from "
               "this service; nothing here can show what was removed afterwards",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
