"""zatca-service — auditable actions.

Onboarding obtains the CSID — the cryptographic identity this taxpayer signs invoices with — so it
is the critical action here: whoever holds it can issue invoices in the customer's name to the tax
authority. Finalising and resubmitting invoices are recorded but never blocked, because the
invoice chain itself is already the tamper-evident record and refusing to issue would stop the
customer trading.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "zatca.settings_updated",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "zatca.invoice_finalized",
        "source": "internal",
        "critical": False,
    },
    {
        "action": "zatca.invoice_resubmitted",
        "source": "admin",
        "critical": False,
    },
    {
        "action": "zatca.swept",
        "source": "system",
        "critical": False,
    },
    {
        "action": "zatca.tail_released",
        "source": "admin",
        "critical": True,
        "resource": "invoice",
        "why": "releasing a reserved ICV deletes the invoice row and frees the number for reuse; "
               "the row is gone the moment it happens, so an unrecorded release cannot be "
               "reconstructed and an unattributable one is indistinguishable from tampering with "
               "the tax number sequence",
    },
    {
        "action": "zatca.invoice_corrected",
        "source": "admin",
        "critical": False,
        "resource": "invoice",
        "why": "correct-and-resign rebuilds a stuck unsigned invoice's draft before signing; the "
               "record shows who supplied the corrected data that was filed under that ICV",
    },
    {
        "action": "zatca.onboarded_compliance",
        "source": "admin",
        "critical": True,
        "why": "onboarding obtains the CSID, the cryptographic identity used to sign this "
               "taxpayer's invoices; whoever holds it can issue invoices in the customer's "
               "name to the tax authority",
    },
    {
        "action": "zatca.onboarded_production",
        "source": "admin",
        "critical": True,
        "why": "production onboarding puts a live signing certificate in place; an "
               "unattributable one means we cannot say who authorised issuing real tax "
               "documents",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
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
    }
]
