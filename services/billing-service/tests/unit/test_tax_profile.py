"""tax_profile — the one judgement about whether a tenant may issue a tax invoice.

These pin the CONTRACT the operator write, the payment, and the render all share: a tax invoice
needs a real calculator, a rate above zero, and a seller registration number — and the FIRST thing
missing is named, because 'incomplete' is not something an operator can act on.
"""
from __future__ import annotations

import pytest

from app.core import tax_profile as tp

_OK = {"tax_driver": "vat", "vat_rate": "0.15", "seller_vat": "300000000000003"}


def test_a_complete_profile_is_complete():
    assert tp.complete(**_OK)
    assert tp.missing_key(**_OK) is None


@pytest.mark.parametrize("mode,is_tax", [
    ("receipt", False), ("tax_invoice", True), ("both", True),
    ("TAX_INVOICE", True), ("", False), (None, False)])
def test_issues_tax(mode, is_tax):
    assert tp.issues_tax(mode) is is_tax


def test_missing_key_names_the_first_gap_in_order():
    # calculator first — nothing else matters if VAT is never computed
    assert tp.missing_key(tax_driver="null", vat_rate="0.15", seller_vat="3") == "TAX_CALCULATOR_DRIVER"
    # then the rate
    assert tp.missing_key(tax_driver="vat", vat_rate="0", seller_vat="3") == "VAT_RATE"
    assert tp.missing_key(tax_driver="vat", vat_rate="bad", seller_vat="3") == "VAT_RATE"
    # then the seller registration — the escalation's named example
    assert tp.missing_key(tax_driver="vat", vat_rate="0.15", seller_vat="") == "INVOICE_SELLER_VAT"
    assert tp.missing_key(tax_driver="vat", vat_rate="0.15", seller_vat="   ") == "INVOICE_SELLER_VAT"


def test_require_complete_raises_named_error():
    with pytest.raises(tp.TaxProfileIncomplete) as ei:
        tp.require_complete(tax_driver="vat", vat_rate="0.15", seller_vat="")
    assert ei.value.missing_key == "INVOICE_SELLER_VAT"
    assert str(ei.value) == "TAX_PROFILE_INCOMPLETE: INVOICE_SELLER_VAT"


def test_check_is_a_noop_for_receipt_mode():
    # a receipt needs none of this — even a wholly empty profile is fine
    tp.check("receipt", tax_driver="null", vat_rate="0", seller_vat="")


def test_check_refuses_tax_mode_on_an_incomplete_profile():
    with pytest.raises(tp.TaxProfileIncomplete) as ei:
        tp.check("tax_invoice", tax_driver="vat", vat_rate="0.15", seller_vat="")
    assert ei.value.missing_key == "INVOICE_SELLER_VAT"


def test_effective_doc_type_degrades_to_receipt_when_incomplete():
    assert tp.effective_doc_type("tax_invoice", **_OK) == "tax_invoice"
    assert tp.effective_doc_type("both", **_OK) == "tax_invoice"
    assert tp.effective_doc_type("receipt", **_OK) == "receipt"
    # mode asks for tax, profile cannot back it → the safe, true, lesser thing
    assert tp.effective_doc_type("tax_invoice", tax_driver="vat", vat_rate="0.15",
                                 seller_vat="") == "receipt"
