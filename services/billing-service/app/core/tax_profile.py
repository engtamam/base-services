"""The one judgement about whether a tenant may issue a TAX invoice — as pure functions.

A tax invoice is a legally binding assertion that VAT was charged by a REGISTERED seller.
Three facts must ALL hold, or the document is void the instant it is issued — a page titled
"tax invoice" over a 0% line with no registration number is not a lesser invoice, it is a
false one, and the customer's accountant finds it before we do:

  * a tax CALCULATOR is wired (not the null driver), so VAT is actually computed;
  * the VAT RATE is greater than zero, so the tax line is real;
  * the seller's VAT REGISTRATION NUMBER is present, so the document names who charged the tax.

The whole defect this closes was that the switch (BILLING_DOCUMENT_MODE) was live-editable while
the profile behind it (calculator, rate, seller number) was read from static settings — so one
click could flip a tenant to `tax_invoice` and the next paid invoice rendered a void document,
and nothing failed, warned, or logged.

This module holds ONLY that judgement, so the three places that MUST agree cannot drift:
  1. the operator write — refuse the switch up front, naming the missing key (`check`);
  2. the moment an invoice is paid — decide what the document IS and freeze it (`effective_doc_type`);
  3. the moment a document is rendered — a SECOND gate, in case the profile was broken after the
     switch (a bad rollout emptied the seller number), degrading to a receipt (`complete`).
"""
from __future__ import annotations

# The document modes that produce a tax invoice at all. `receipt` never touches VAT/ZATCA.
_TAX_MODES = ("tax_invoice", "both")
# A tax CALCULATOR that computes nothing — treated as "no calculator" for completeness.
_NULL_DRIVERS = ("", "null", "none")


class TaxProfileIncomplete(ValueError):
    """A tax invoice was asked for from a profile that cannot back one.

    Carries the FIRST missing key BY NAME, because "incomplete" is not something an operator can
    act on — `TAX_PROFILE_INCOMPLETE: INVOICE_SELLER_VAT` is.
    """

    def __init__(self, missing_key: str) -> None:
        self.missing_key = missing_key
        super().__init__(f"TAX_PROFILE_INCOMPLETE: {missing_key}")


def issues_tax(mode: str | None) -> bool:
    """Does this document mode produce a tax invoice at all?"""
    return str(mode or "").strip().lower() in _TAX_MODES


def _rate(vat_rate: object) -> float:
    try:
        return float(vat_rate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def missing_key(*, tax_driver: object, vat_rate: object, seller_vat: object) -> str | None:
    """The FIRST fact a tax invoice needs and does not have, or None if the profile is complete.

    Order is deliberate — the calculator first (nothing else matters if VAT is never computed),
    then the rate, then the registration number — so the name returned is the most foundational
    thing to fix.
    """
    if str(tax_driver or "").strip().lower() in _NULL_DRIVERS:
        return "TAX_CALCULATOR_DRIVER"
    if _rate(vat_rate) <= 0:
        return "VAT_RATE"
    if not str(seller_vat or "").strip():
        return "INVOICE_SELLER_VAT"
    return None


def complete(*, tax_driver: object, vat_rate: object, seller_vat: object) -> bool:
    return missing_key(tax_driver=tax_driver, vat_rate=vat_rate, seller_vat=seller_vat) is None


def require_complete(*, tax_driver: object, vat_rate: object, seller_vat: object) -> None:
    """Raise TaxProfileIncomplete(<first missing key>), else return None."""
    k = missing_key(tax_driver=tax_driver, vat_rate=vat_rate, seller_vat=seller_vat)
    if k is not None:
        raise TaxProfileIncomplete(k)


def check(mode: str | None, *, tax_driver: object, vat_rate: object, seller_vat: object) -> None:
    """Operator-write gate: refuse a mode that ISSUES a tax invoice from an incomplete profile.

    A no-op for a receipt mode — a receipt needs none of this. This is what makes the whole tax
    profile atomic at the write: whether the caller is flipping the MODE to tax or lowering one of
    the other keys under a mode that is already tax, the resulting profile is evaluated together.
    """
    if issues_tax(mode):
        require_complete(tax_driver=tax_driver, vat_rate=vat_rate, seller_vat=seller_vat)


def effective_doc_type(mode: str | None, *, tax_driver: object, vat_rate: object,
                       seller_vat: object) -> str:
    """What a paid invoice under `mode` actually BECOMES, given the profile behind it.

    `tax_invoice` only when the mode asks for tax AND the profile can back it; otherwise `receipt`.
    This is the fail-safe: the document degrades to the true, lesser thing (a payment receipt)
    rather than emit a false greater one.
    """
    if issues_tax(mode) and complete(tax_driver=tax_driver, vat_rate=vat_rate, seller_vat=seller_vat):
        return "tax_invoice"
    return "receipt"
