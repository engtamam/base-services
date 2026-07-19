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
        # Reading the log is itself an investigative act. "Who looked up this person, and when"
        # is a question that gets asked after a leak, and until now this service — the one whose
        # entire job is answering that question about everyone else — could not answer it about
        # itself. An unlogged read is how someone finds out what we know about them, or checks
        # whether their own tracks are visible, and leaves nothing behind.
        "action": "audit.queried",
        "source": "user",
        # NOT critical, deliberately. Critical means "refuse the action when the log is down", and
        # refusing READS during an audit outage locks investigators out of the log at exactly the
        # moment something is going wrong. A read changes nothing, so the safer failure is to
        # serve it and lose one access record — the opposite trade from the export below.
        "critical": False,
    },
    {
        "action": "audit.exported",
        "source": "user",
        "critical": True,
        "why": "export takes the log out in BULK, which is the shape of exfiltration rather than "
               "investigation — an unrecorded bulk extraction of every action on the platform is "
               "worse than a refused one, so this fails closed where a paged query does not",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
