"""Erasure — billing: the invoice STAYS, and during retention it stays UNALTERED.

A person asks to be deleted. Tax law says the invoice must survive 6–10 years (ZATCA; equivalents
everywhere). Privacy law says the person must not. Both are mandatory, so "delete the invoice" is
illegal — and, during the retention window, "edit the invoice" is wrong too.

  * `buyer_name` is a MANDATORY field on a standard (B2B) ZATCA tax invoice, alongside the VAT number.
    The retained copy is the record as legally ISSUED: an auditor, ZATCA, or the shop in a dispute is
    entitled to see it EXACTLY as filed. Pseudonymising the name during the retention window makes our
    retained copy no longer match the issued document — for a field the law never asked us to touch.
    This is the same principle zatca-service applies to signed_xml/cleared_xml: touch nothing, because
    the retained document's integrity as issued is itself the legal requirement.

  * So during retention we retain the invoice IN FULL and UNALTERED — buyer_name included. Only AFTER
    `created + retention_years` has passed does the statutory hold lapse and the buyer's name become
    eligible for pseudonymisation (a retention sweep may also delete the row then, if nothing else
    needs it). Pseudonymising is not erasure and the retention obligation covers the record as a whole,
    so there is no legal basis to edit the mandatory field EARLY — only to keep it.

  * `buyer_vat` / `buyer_cr` — a VAT number and a commercial registration identify a BUSINESS, and a
    tax invoice is legally required to carry them; retained under tax_retention, never erased.

  * The money ledger — immutable by construction and already keyed by tenant, not by person.

Anything kept names its basis. "We kept it" is an answer with a reason attached, never a silence:
a person told "we deleted you" while their name sits on an invoice has been lied to; a person told
"we kept your invoice, unaltered, here is the law that requires it" has been answered.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.models import now_ts
from app.core.pii import subject_ref

_INVOICES = "invoices"
_SUBSCRIPTIONS = "subscriptions"

RETENTION_BASIS = "tax_retention"


def _retention_expiry_ts(created_iso: Any, years: int) -> float | None:
    """Epoch second at which `created + years` falls, calendar-accurate. None if the issue date is
    unparseable — the caller treats that as STILL WITHIN retention (fail safe: keep the record intact
    rather than risk editing a legally-retained field on a row whose age we cannot establish)."""
    if not created_iso or not isinstance(created_iso, str):
        return None
    try:
        c = _dt.datetime.fromisoformat(created_iso)
    except ValueError:
        return None
    try:
        exp = c.replace(year=c.year + years)
    except ValueError:                    # 29 Feb in a non-leap target year -> 1 Mar
        exp = c.replace(year=c.year + years, month=3, day=1)
    return exp.timestamp()


class BillingErasure:
    service = "billing-service"
    category = "invoices"

    def __init__(self, *, store, pii_salt: str, retention_years: int = 10, clock=now_ts) -> None:
        self._store = store
        self._salt = pii_salt
        self._years = retention_years
        self._clock = clock               # injected so a test can place an invoice past retention

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        now = self._clock()
        matched = 0                # invoices carrying the buyer's name
        retained_unaltered = 0     # within retention: touched nothing
        pseudonymised = 0          # past retention: name replaced
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

                expiry = _retention_expiry_ts(inv.get("created"), self._years)
                if expiry is None or now < expiry:
                    # WITHIN the mandatory retention window (or an undatable row): the invoice — its
                    # buyer_name included — is the record as issued. Touch nothing; retain it in full.
                    retained_unaltered += 1
                    continue

                # Retention has expired: the statutory hold has lapsed and the privacy right now
                # applies. The human's name goes; the financial record does not.
                inv["buyer_name"] = ref
                await self._store.put(_INVOICES, inv["id"], inv)
                pseudonymised += 1
            if not cursor:
                break

        retained: list[dict[str, str]] = []
        if retained_unaltered:
            retained.append({
                "what": f"invoices:{retained_unaltered}",
                "basis": RETENTION_BASIS,
                "why": (f"a tax invoice must survive ~{self._years} years by law and is retained IN "
                        f"FULL and UNALTERED during that window — the buyer's NAME included, because "
                        f"it is a MANDATORY field on a standard ZATCA tax invoice and the retained "
                        f"copy must match the document exactly as issued; the name becomes eligible "
                        f"for pseudonymisation only once retention expires"),
            })
        if pseudonymised:
            retained.append({
                "what": f"invoices:{pseudonymised}",
                "basis": RETENTION_BASIS,
                "why": (f"retention (~{self._years}y) has expired for these invoices; the buyer's NAME "
                        f"is now pseudonymised while the financial record is kept"),
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
