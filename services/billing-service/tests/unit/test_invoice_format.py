"""Turning stored values into what a person reads on a tax document.

The first test in this file is the whole reason it exists: the PDF was printing minor units raw,
so an invoice for 115.00 SAR read `11500`. That is a mis-stated tax document, and nothing caught it
because nothing ever asserted what the document said.
"""
from __future__ import annotations

import pytest

from app.core.invoice_format import apportion_vat, exponent, money, percent


# ── the defect this replaced ──────────────────────────────────────────────────
def test_minor_units_are_scaled_before_a_person_sees_them():
    assert money(11500, "SAR") == "115.00 SAR"


def test_thousands_are_grouped():
    assert money(126500000, "SAR") == "1,265,000.00 SAR"


# ── currency exponents ────────────────────────────────────────────────────────
@pytest.mark.parametrize("currency,minor,expected", [
    ("SAR", 11500, "115.00 SAR"),
    ("USD", 11500, "115.00 USD"),
    # Three decimals, and four of these are Gulf currencies — not an edge case for this platform.
    ("KWD", 11500, "11.500 KWD"),
    ("BHD", 11500, "11.500 BHD"),
    ("OMR", 11500, "11.500 OMR"),
    ("JOD", 11500, "11.500 JOD"),
    # No minor unit at all.
    ("JPY", 11500, "11,500 JPY"),
    ("KRW", 11500, "11,500 KRW"),
])
def test_each_currency_uses_its_own_exponent(currency, minor, expected):
    """Assuming two decimals everywhere overstates a Kuwaiti invoice tenfold and understates a
    Japanese one by a hundred."""
    assert money(minor, currency) == expected


def test_an_unknown_currency_falls_back_to_two_places():
    assert exponent("ZZZ") == 2


def test_currency_matching_is_case_insensitive():
    assert money(11500, "kwd") == "11.500 KWD"


# ── edges ─────────────────────────────────────────────────────────────────────
def test_a_negative_amount_keeps_its_sign_in_front():
    """A credit note reading (115.00) has been mistaken for a positive amount often enough that
    the sign is worth the plainness."""
    assert money(-11500, "SAR") == "-115.00 SAR"


def test_an_amount_smaller_than_one_unit_is_padded():
    assert money(5, "SAR") == "0.05 SAR"
    assert money(5, "KWD") == "0.005 KWD"


def test_zero_and_junk_do_not_raise():
    for bad in (None, "", "abc", []):
        assert money(bad, "SAR") == "0.00 SAR"


def test_a_very_large_amount_keeps_every_digit():
    # Formatting via float division loses precision here; string slicing does not.
    assert money(9_007_199_254_740_993_00, "SAR") == "9,007,199,254,740,993.00 SAR"


# ── VAT rate ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [("0.15", "15%"), (0.15, "15%"), (15, "15%"),
                                          ("0.05", "5%"), ("", ""), (None, "")])
def test_a_rate_reads_as_a_percentage_however_it_was_stored(raw, expected):
    """A rate printed as "0.15%" gets an invoice rejected rather than queried."""
    assert percent(raw) == expected


# ── apportionment ─────────────────────────────────────────────────────────────
def test_line_vat_always_sums_to_the_invoice_vat():
    """An invoice whose lines do not add up to its total is one a buyer is entitled to reject."""
    lines = [{"amount_minor": 3333}, {"amount_minor": 3333}, {"amount_minor": 3334}]
    shares = apportion_vat(lines, tax_minor=1500, taxable_minor=10000)
    assert sum(shares) == 1500


def test_the_rounding_remainder_lands_on_the_largest_line():
    lines = [{"amount_minor": 100}, {"amount_minor": 9900}]
    shares = apportion_vat(lines, tax_minor=1501, taxable_minor=10000)
    assert sum(shares) == 1501
    assert shares[1] > shares[0]


def test_no_lines_and_no_tax_are_handled():
    assert apportion_vat([], tax_minor=0, taxable_minor=0) == []
    assert apportion_vat([{"amount_minor": 100}], tax_minor=0, taxable_minor=100) == [0]


def test_a_zero_taxable_total_does_not_divide_by_zero():
    assert apportion_vat([{"amount_minor": 0}], tax_minor=100, taxable_minor=0) == [100]


# Date formatting moved to the ReportLab renderer's Arabic `_issued` — see test_invoice_pdf.py.
