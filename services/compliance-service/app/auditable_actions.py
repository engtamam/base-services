"""compliance-service — auditable actions.

The service that proves the platform obeys the law has to be able to prove what IT did. Two
families are critical: anything that changes what we are allowed to keep (consent withdrawal, a
legal hold placed or released), and anything that destroys data (processing a DSAR, running
retention).

Releasing a hold is critical for a reason that is easy to miss: while a hold stands, data cannot
be deleted. Releasing it re-enables deletion, so an unattributable release is how held evidence
quietly becomes deletable.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "consent.granted",
        "source": "user",
        "critical": False,
    },
    {
        "action": "dsar.created",
        "source": "user",
        "critical": False,
    },
    {
        "action": "dsar.verified",
        "source": "user",
        "critical": False,
    },
    {
        "action": "data_target.registered",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "ropa.recorded",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "data_target.deleted",
        "source": "admin",
        "critical": True,
        "why": "the data map DRIVES the erasure fan-out, so removing a target means the next "
               "DSAR reaches fewer stores and still reports 'complete' — an unattributable "
               "removal is how erasure quietly narrows and nobody can say when it happened",
    },
    {
        "action": "ropa_activity.deleted",
        "source": "admin",
        "critical": True,
        "why": "Art. 30 obliges the controller to MAINTAIN the record of processing; an entry "
               "that vanishes with no trace is the one outcome that cannot be shown to a regulator",
    },
    {
        "action": "dsar.target_acked",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "consent.guardian_recorded",
        "source": "user",
        "critical": True,
        "why": "consent given by someone OTHER than the subject is the claim most likely to be "
               "challenged — a parent, a carer, an attorney — and without a record of who agreed "
               "and on what relation, we cannot show the person was entitled to agree",
    },
    {
        "action": "consent.withdrawn",
        "source": "user",
        "critical": True,
        "why": "processing after a withdrawal is the violation; the withdrawal time is what "
               "decides whether everything that happened afterwards was lawful",
    },
    {
        "action": "dsar.processed",
        "source": "admin",
        "critical": True,
        "why": "processing fans erasure out across every service and destroys data "
               "irreversibly; nothing else can show who authorised it",
    },
    {
        "action": "legal_hold.placed",
        "source": "admin",
        "critical": True,
        "why": "a hold overrides erasure; if we cannot show when it began we cannot defend "
               "having kept the data",
    },
    {
        "action": "legal_hold.released",
        "source": "admin",
        "critical": True,
        "why": "releasing a hold RE-ENABLES deletion of data that was protected — an "
               "unattributable release is how held evidence quietly becomes deletable",
    },
    {
        "action": "breach.recorded",
        "source": "admin",
        "critical": True,
        "why": "the discovery time starts a regulated notification clock (72h); the record is "
               "what proves we met it",
    },
    {
        "action": "breach.notified",
        "source": "admin",
        "critical": True,
        "why": "notifying the regulator is the legal obligation itself; without a record we "
               "cannot prove we did it",
    },
    {
        "action": "retention.purged",
        "source": "system",
        "critical": True,
        "why": "retention deletes records permanently; an unrecorded purge cannot be "
               "distinguished from data loss",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
]
