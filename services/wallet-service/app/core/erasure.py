"""Erasure — wallet: there is no person here to erase, and that is a DESIGN property, not luck.

Every row this service owns is keyed by TENANT. `Wallet`, `LedgerEntry`, `Hold` and `Grant` carry
funds, amounts and sequence numbers — no name, no email, no user id, not even a subject_ref. Money
in this platform belongs to an organisation, and the human who happened to spend it is recorded in
audit-service (pseudonymised) rather than here.

So this endpoint exists to answer a question, not to delete rows. It matters that it exists:

  * A DSAR must be able to prove it ASKED every service. "wallet-service was never contacted" and
    "wallet-service holds nothing" look identical in an incomplete system and completely different
    to a regulator. Answering `already_absent` is evidence; silence is a gap.

  * If wallet ever gains a person-linked field, this file is where that decision becomes visible.
    A service with no erase endpoint quietly accumulates PII; a service with one has to change this
    code to do it, and the change shows up in review.

Note what would happen if a subject WERE linked here: the ledger is immutable by construction and
financial records carry a statutory retention duty of their own, so the answer would be billing's —
retain the record, pseudonymise the person — never deletion. That is why the basis below is stated
even though nothing is currently retained under it.

Idempotent and free: no scan, no writes, no I/O. A DSAR retries and this always answers the same.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

FINANCIAL_BASIS = "statutory_financial_retention"


class WalletErasure:
    service = "wallet-service"
    category = "wallet_balances"

    def __init__(self, *, store=None, pii_salt: str) -> None:
        self._store = store          # unused today; present so a future linkage cannot skip review
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": subject_ref(self._salt, tenant, subject),
            "erased": [],
            "retained": [],
            "already_absent": True,
            "note": ("wallet holds no subject-linked data: every row (wallets, ledger, holds, "
                     "grants) is keyed by tenant. Were a person ever linked here, the ledger is "
                     f"immutable and would be retained under {FINANCIAL_BASIS} and pseudonymised "
                     "rather than deleted."),
        }
