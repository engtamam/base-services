"""wallet-service — auditable actions.

Every action here moves money, so the split is by DIRECTION rather than by importance. Credits and
refunds create value out of nothing — that is the insider-fraud path and it is critical. Spends,
reserves and confirmations consume value the customer already had, and the immutable ledger
already records them, so they never block.

See services/_template/app/auditable_actions.py for the format, the four sources, and how to decide
`critical` (it REFUSES the request when the audit log is unavailable, so it is a behaviour and not
a severity label).
"""
from __future__ import annotations

from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = [
    {
        "action": "wallet.spent",
        "source": "user",
        "critical": False,
    },
    {
        "action": "wallet.reserved",
        "source": "user",
        "critical": False,
    },
    {
        "action": "wallet.confirmed",
        "source": "user",
        "critical": False,
    },
    {
        "action": "wallet.released",
        "source": "user",
        "critical": False,
    },
    {
        "action": "wallet.holds_swept",
        "source": "system",
        "critical": False,
    },
    {
        "action": "wallet.grants_swept",
        "source": "system",
        "critical": False,
    },
    {
        # A dormant (expired-but-preserved) grant's remainder was restored to a customer's spendable
        # balance. Money moving BACK into a balance is privileged — worth a durable row naming which
        # grant and how much — but not critical: a reclaim that could not be recorded is retried, and
        # blocking it would keep a customer from their preserved funds.
        "action": "wallet.grant_reclaimed",
        "source": "system",
        "critical": False,
    },
    {
        "action": "wallet.credited",
        "source": "admin",
        "critical": True,
        "why": "a credit creates value from nothing; an unattributable credit is the classic "
               "insider-fraud path and is indistinguishable from theft",
    },
    {
        "action": "wallet.refunded",
        "source": "admin",
        "critical": True,
        "why": "a refund returns money that was already taken; without a record there is no "
               "way to say who authorised it",
    },
    {
        "action": "wallet.inspected",
        "source": "admin",
        "critical": False,
        "why": "a platform operator read a specific tenant's balance, statement or reconciliation — "
               "cross-tenant access to a customer's money data is exactly what a security review "
               "asks about: who looked at whose wallet, and when. Not critical: it is a read, and "
               "refusing it during an audit outage protects nothing",
    },
    {
        "action": "subject.erased",
        "source": "internal",
        "critical": True,
        "why": "erasure is irreversible and the data is gone — nothing but this record can "
               "show we honoured the DSAR, or prove we erased the right person",
    },
    {
        "action": "wallet.withdrawn",
        "source": "internal",
        "critical": True,
        "why": "money leaves the wallet — a withdrawal is a debit against a customer's balance, so "
               "an unrecorded one is unreconstructable and an unattributable one is indistinguishable "
               "from theft",
    },
    {
        "action": "wallet.withdrawal_settled",
        "source": "internal",
        "critical": True,
        "why": "settlement is the moment a withdrawal becomes final (or reverses on failure); which "
               "withdrawal settled and when is the record that proves the funds actually moved as "
               "the ledger claims",
    },
]
