"""api-gateway — auditable actions.

The gateway routes and aggregates; it owns no data of its own, so it has exactly one auditable
action. It still declares and emits it, because a DSAR must be able to prove it asked — 'never
contacted' and 'holds nothing' are indistinguishable in an incomplete system and completely
different to a regulator.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
