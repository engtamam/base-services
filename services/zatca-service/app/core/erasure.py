"""Erasure — zatca: the invoice is UNTOUCHABLE, and saying so plainly is the honest answer.

Billing pseudonymises the buyer's name on an invoice it must keep. This service cannot even do
that, and the reason is worth stating precisely, because "we refuse" without a reason is what a
regulator reads as non-compliance:

  1. **The invoice is cryptographically signed.** `signed_xml` carries a ZATCA signature over its
     own bytes. Change one character of the buyer's name and the signature no longer verifies —
     the invoice stops being a valid tax document.

  2. **The invoices are hash-chained.** Each invoice's hash becomes the next one's PIH (ICV/PIH
     chain). Altering invoice #400 does not damage one record; it invalidates #401 through the end
     of the taxpayer's history. The blast radius of a single edit is every later invoice.

  3. **`cleared_xml` is ZATCA's own stamped copy.** It is the tax authority's document, held by us.
     It is not ours to edit.

So: the legal record is retained, in full, unaltered, and the ack SAYS so with the basis attached.
A person told "we deleted you" while their name sits inside a signed invoice has been lied to. A
person told "your invoice is kept because tax law requires it for ~6 years, and here is why it
cannot even be pseudonymised" has been answered — and that answer is one a regulator accepts.

What we CAN erase, and do: the `draft` working copy. Before an invoice is signed, `draft` holds the
unsigned UBL used to build and re-sign it. Once the invoice reaches an accepted state that copy is
dead weight, it is not part of the legal record, it is not signed, and nothing in the chain
references it — but it still contains the buyer's identity in plain XML. Dropping it removes a real
copy of the person's data while leaving every legally-required byte exactly where it was.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.models import ACCEPTED
from app.core.pii import subject_ref

_INVOICES = "zatca_invoices"
_PAGE = 500

TAX_BASIS = "statutory_tax_retention"
SIGNATURE_BASIS = "cryptographic_integrity"


class ZatcaErasure:
    service = "zatca-service"
    category = "tax_invoices"

    def __init__(self, *, store, pii_salt: str, retention_years: int = 6,
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._years = retention_years
        self._max = max_scan

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        drafts_dropped = 0
        legal_records = 0
        scanned = 0

        cursor = None
        while scanned < self._max:
            rows, cursor = await self._store.find(_INVOICES, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("created", "asc"))
            if not rows:
                break
            for inv in rows:
                scanned += 1
                if not _mentions(inv.get("draft"), subject):
                    continue
                legal_records += 1
                # Only once the invoice is final: before that the draft is how it gets signed (and
                # re-signed after a signer outage), so dropping it would strand a live invoice.
                if inv.get("status") in ACCEPTED and inv.get("draft"):
                    inv["draft"] = {k: v for k, v in inv["draft"].items() if k != "unsigned_xml"}
                    await self._store.put(_INVOICES, inv["id"], inv)
                    drafts_dropped += 1
            if not cursor:
                break

        erased: list[str] = []
        if drafts_dropped:
            erased.append(f"invoice_draft_xml:{drafts_dropped}")

        retained: list[dict[str, str]] = []
        if legal_records:
            retained.append({
                "what": f"tax_invoices:{legal_records}",
                "basis": TAX_BASIS,
                "why": (f"a tax invoice must survive ~{self._years} years by law; it is retained "
                        f"in full and unaltered"),
            })
            retained.append({
                "what": f"buyer_identity_in_signed_xml:{legal_records}",
                "basis": SIGNATURE_BASIS,
                "why": ("the buyer's identity is inside signed_xml/cleared_xml, which carries a "
                        "ZATCA signature over its own bytes and whose hash is the NEXT invoice's "
                        "PIH — pseudonymising it would break the signature and invalidate every "
                        "later invoice in the taxpayer's chain, so unlike billing this data "
                        "cannot even be pseudonymised, only retained"),
            })

        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": legal_records == 0,
            "truncated": scanned >= self._max,
        }


def _mentions(draft: Any, subject: str) -> bool:
    """Does this invoice's working copy carry the subject's identity?

    zatca has no subject column — the buyer lives inside the UBL — so matching is a containment
    check over the draft. It errs towards MATCHING: a false positive costs one dead working copy,
    while a false negative leaves the person's data in place. Only the draft is ever inspected, so
    a match can never lead to touching the signed record.
    """
    if not isinstance(draft, dict) or not subject:
        return False
    xml = draft.get("unsigned_xml") or ""
    return subject in xml
