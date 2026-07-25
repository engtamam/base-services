"""onboarding-service — auditable actions.

Two things here are worth recording, and they are the two that hand something over.

A GRANT is money-like value leaving the platform. A FORCE-COMPLETE is an operator asserting by hand
exactly what a requirement exists to verify — the closest thing this service has to a master key.
Both are disputed later or not at all, and "later" is when the record has to already exist.

Evaluating a flow is not audited: it happens on every page load, and burying the log in reads would
make the two entries above harder to find, not easier.
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "grant.issued",
        "source": "user",
        "critical": True,
        "resource": "grant",
        "why": "a grant is money-like value leaving the platform; without a record we cannot say "
               "who received what, on which identity, or whether the same identity was paid twice",
    },
    {
        "action": "flow.force_completed",
        "source": "admin",
        "critical": True,
        "resource": "onboarding_flow",
        "why": "forcing a requirement asserts by hand what the requirement exists to verify — it "
               "is the one path that satisfies a check without the underlying fact being true, so "
               "an unattributable force is indistinguishable from an attacker with an operator token",
    },
    {
        "action": "provisioning.compensated",
        "source": "system",
        "critical": False,
        "resource": "onboarding_flow",
    },
    {
        "action": "provisioning.resumed",
        "source": "admin",
        "critical": False,
        "resource": "subject",
        "why": "an operator re-ran the compensation a failed provisioning could not finish for a "
               "customer; it acts against downstream services on their behalf, so who resumed whose "
               "cleanup is worth the record. Not critical: it is idempotent and re-runnable",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "resource": "subject",
        "why": "erasure is irreversible and the data is gone — nothing but this record can show we "
               "honoured the DSAR, or prove we erased the right person",
    },
]
