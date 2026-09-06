"""Erasure — billing: the invoice STAYS, UNALTERED, forever.

A person asks to be deleted. Tax law says the invoice must survive (ZATCA: 6 years, 11 for movable
capital assets, 15 for immovable). Privacy law (PDPL) says the person must not — but PDPL exempts
data another law requires kept. Both point the same way for a tax invoice: never delete it, and
never edit it.

  * A ZATCA Phase-2 tax invoice is SEALED at issuance with a cryptographic signature so its details —
    `buyer_name` included (a MANDATORY field) — cannot be altered afterwards without breaking that
    signature. The retained copy is the record as legally ISSUED, and its integrity as issued is
    itself the requirement. So we touch NOTHING — not on a DSAR, not after any retention window.
    (This service once pseudonymised the buyer_name once retention lapsed; that ALTERS a sealed tax
    document, which ZATCA forbids — removed. The invoice is now retained in full and unaltered
    indefinitely.)

  * `buyer_vat` / `buyer_cr` — a VAT number and a commercial registration identify a BUSINESS, and a
    tax invoice is legally required to carry them; retained under tax_retention, never erased.

  * The money ledger — immutable by construction and already keyed by tenant, not by person.

Anything kept names its basis. "We kept it" is an answer with a reason attached, never a silence:
a person told "we deleted you" while their name sits on an invoice has been lied to; a person told
"we kept your invoice, unaltered, here is the law that requires it" has been answered.
"""
from __future__ import annotations

from typing import Any

from app.core.models import now_ts
from app.core.pii import subject_ref

_INVOICES = "invoices"
_SUBSCRIPTIONS = "subscriptions"

RETENTION_BASIS = "tax_retention"


class BillingErasure:
    service = "billing-service"
    category = "invoices"

    def __init__(self, *, store, pii_salt: str, retention_years: int = 10, clock=now_ts) -> None:
        self._store = store
        self._salt = pii_salt
        # retention_years/clock are accepted for backward-compat but no longer used: a ZATCA tax
        # invoice is never altered (no retention-expiry branch to time) — it is retained as issued.
        _ = (retention_years, clock)

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        matched = 0                # invoices carrying the buyer's name — retained UNALTERED, always
        kept_business_ids = 0

        cursor = None
        while True:
            rows, cursor = await self._store.find(_INVOICES, {"tenant": tenant},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for inv in rows:
                name = inv.get("buyer_name")
                if not name or name == ref:
                    continue
                matched += 1
                if inv.get("buyer_vat") or inv.get("buyer_cr"):
                    kept_business_ids += 1
                # A ZATCA tax invoice is CRYPTOGRAPHICALLY SEALED at issuance: its details — the buyer's
                # name included (a mandatory field) — cannot be altered afterwards without breaking the
                # signature, and the record must be preserved exactly as issued. So we NEVER touch it —
                # not on a DSAR, not after any retention window. PDPL's own exemption for data another
                # law requires to be kept covers this: the invoice is retained in full, unaltered,
                # indefinitely. (Earlier this pseudonymised the name once retention lapsed — that was an
                # ALTERATION of a sealed tax document, which ZATCA forbids; removed.)
            if not cursor:
                break

        retained: list[dict[str, str]] = []
        if matched:
            retained.append({
                "what": f"invoices:{matched}",
                "basis": RETENTION_BASIS,
                "why": ("a ZATCA tax invoice is sealed at issuance and must be preserved EXACTLY as "
                        "issued — the buyer's NAME included (a mandatory field); altering it would "
                        "break the invoice's cryptographic integrity. Retained in full and UNALTERED "
                        "indefinitely under the tax-law obligation PDPL itself exempts from erasure"),
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
            "already_absent": matched == 0,
        }
