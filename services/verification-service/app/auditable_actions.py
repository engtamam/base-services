"""verification-service — auditable actions.

This service grants TRUST. Everything downstream — a welcome gift, a business account, a raised
limit — is issued because something here said "verified". So the audit question is not "was data
changed" but "who decided this person is who they say", and that is exactly what a dispute or a
fraud investigation turns on.

Only affirmations and human decisions are critical. A refusal that cannot be recorded should still
refuse: blocking someone AND failing to log it is bad, but refusing to refuse them because the
audit log is down is worse — it turns a logging outage into an open door.

See services/_template/app/auditable_actions.py for the format and how to decide `critical`.
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "identity.verified",
        "source": "user",
        "critical": True,
        "resource": "verification",
        "why": "affirming an identity is granting trust, and every downstream privilege is issued "
               "on the strength of it; without a record we cannot say who was verified, by which "
               "provider, or on what evidence when it is later disputed",
    },
    {
        "action": "review.decided",
        "source": "admin",
        "critical": True,
        "resource": "verification",
        "why": "a human granting or refusing trust is the most privileged act in this service — an "
               "automated check can only repeat a registry, a reviewer can approve anyone. An "
               "appeal needs to know who decided and what they were looking at",
    },
    {
        "action": "identity.refused",
        "source": "user",
        "critical": False,
        "resource": "verification",
    },
    {
        "action": "identity.check_failed",
        "source": "system",
        "critical": False,
        "resource": "verification",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "resource": "subject",
        "why": "erasure is irreversible and the data is gone — nothing but this record can show we "
               "honoured the DSAR, or prove we erased the right person",
    },
    {
        "action": "operator.secret_set",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "a credential change is the action that most needs attribution: the previous value "
               "is gone the moment it happens, so an unrecorded rotation cannot be reconstructed, "
               "and an unattributable one is indistinguishable from an attacker's",
    },
    {
        "action": "operator.secret_cleared",
        "source": "admin",
        "critical": True,
        "resource": "secret",
        "why": "clearing a credential silently reverts the service to whatever the environment "
               "holds, which is a behaviour change nobody would otherwise see",
    },
    {
        "action": "operator.config_changed",
        "source": "admin",
        "critical": False,
        "resource": "config",
        "why": "policy changes explain later behaviour — why a sweep deleted more, why a limit "
               "started refusing — and that history is unavailable anywhere else. Not critical: "
               "refusing to change a retention period because the log is down protects nothing",
    },
]
