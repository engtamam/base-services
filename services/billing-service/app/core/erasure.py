"""Erasure — billing: the invoice STAYS. This is the case the whole phase was written for.

A person asks to be deleted. Tax law says the invoice must survive 6–10 years (ZATCA; equivalents
everywhere). Privacy law says the person must not. Both are mandatory, so "delete the invoice" is
illegal and "keep the buyer's name on it" is also illegal.

The answer is neither: **keep the record, pseudonymise the person on it.**

  * The invoice, its line items, its totals, its tax — retained, untouched, still auditable by a
    tax inspector who does not need to know who the buyer was to check the arithmetic.
  * `buyer_name` — replaced with the `subject_ref`. The financial fact survives; the human does not.
  * `buyer_vat` / `buyer_cr` — a VAT number and a commercial registration are the identifiers of a
    BUSINESS, and a tax invoice is legally required to carry them. They are retained under
    tax_retention, not erased, because erasing them would invalidate the invoice itself.
    For a B2C invoice they are empty and nothing is retained.
  * The money ledger — immutable by construction and already keyed by tenant, not by person.

Anything kept names its basis. "We kept it" is an answer with a reason attached, never a silence:
a person told "we deleted you" while their name sits on an invoice has been lied to, and a person
told "we kept your invoice, here is the law that requires it" has been answered.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

_INVOICES = "invoices"
_SUBSCRIPTIONS = "subscriptions"

RETENTION_BASIS = "tax_retention"


class BillingErasure:
    service = "billing-service"
    category = "invoices"

    def __init__(self, *, store, pii_salt: str, retention_years: int = 10) -> None:
        self._store = store
        self._salt = pii_salt
        self._years = retention_years

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        pseudonymised = 0
        kept_business_ids = 0

        cursor = None
        while True:
            rows, cursor = await self._store.find(_INVOICES, {"tenant": tenant},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for inv in rows:
                if inv.get("buyer_name") in ("", ref) or not inv.get("buyer_name"):
                    continue
                # The human's name goes. The invoice, its totals and its tax do not.
                inv["buyer_name"] = ref
                if inv.get("buyer_vat") or inv.get("buyer_cr"):
                    # A VAT number identifies a BUSINESS, and a tax invoice is legally required to
                    # carry it. Removing it would invalidate the invoice we are obliged to keep.
                    kept_business_ids += 1
                await self._store.put(_INVOICES, inv["id"], inv)
                pseudonymised += 1
            if not cursor:
                break

        retained: list[dict[str, str]] = []
        if pseudonymised:
            retained.append({
                "what": f"invoices:{pseudonymised}",
                "basis": RETENTION_BASIS,
                "why": (f"a tax invoice must survive ~{self._years} years by law; the buyer's NAME "
                        f"is replaced with a pseudonym, the financial record is untouched"),
            })
        if kept_business_ids:
            retained.append({
                "what": f"buyer_vat/buyer_cr:{kept_business_ids}",
                "basis": RETENTION_BASIS,
                "why": ("a VAT number and a commercial registration identify a BUSINESS, and a tax "
                        "invoice is legally required to carry them — removing them would invalidate "
                        "the invoice we are obliged to keep"),
            })

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [],          # billing deletes nothing: everything here is a legal record
            "retained": retained,
            "already_absent": pseudonymised == 0,
        }
