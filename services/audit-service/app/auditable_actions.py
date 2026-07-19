"""audit-service — auditable actions.

The log auditing itself needs one rule stated plainly: **ingest is NOT declared here.** Recording
an audit event about recording an audit event is an infinite regress, and the chain already holds
that fact by existing.

What IS audited is anything that REMOVES from the log. Deleting audit records is the single most
suspicious operation on this platform, so both paths that can do it are critical.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "audit.purged",
        "source": "admin",
        "critical": True,
        "why": "purging deletes audit records — the most suspicious operation on the platform, "
               "and the one an attacker covering their tracks would reach for first",
    },
    {
        "action": "audit.retention_enforced",
        "source": "system",
        "critical": True,
        "why": "retention deletes audit records on a schedule; without a record of the run we "
               "cannot distinguish lawful expiry from someone quietly trimming the evidence",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
