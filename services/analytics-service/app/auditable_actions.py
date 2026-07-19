"""analytics-service — auditable actions.

Behavioural events arrive here in volume, so the ingest path is deliberately NOT audited: one
audit event per analytics event would double the platform's write load to record something the
analytics store already holds.

What is critical is data leaving — an export is the difference between a customer and a breach —
and the prune job, because it deletes.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "analytics.queried",
        "source": "user",
        "critical": False,
    },
    {
        "action": "analytics.report_created",
        "source": "user",
        "critical": False,
    },
    {
        "action": "analytics.report_deleted",
        "source": "user",
        "critical": False,
    },
    {
        "action": "analytics.exported",
        "source": "user",
        "critical": True,
        "why": "a bulk export of behavioural data is the difference between a customer using "
               "the product and a breach; without a record we cannot say who took what, or "
               "when",
    },
    {
        "action": "analytics.raw_pruned",
        "source": "system",
        "critical": True,
        "why": "the prune job deletes raw events permanently; an unrecorded deletion cannot be "
               "distinguished from data loss, and the events themselves are gone",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
