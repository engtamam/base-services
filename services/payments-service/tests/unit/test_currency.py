"""ISO-4217 minor-unit conversion — exponent-correct for 2-dp, 3-dp and 0-dp currencies (a hardcoded
/100 mischarges KWD/JPY). L6 + M3."""
from __future__ import annotations

from decimal import Decimal

from app.core.currency import SUPPORTED_CURRENCIES, exponent, to_major, to_minor


def test_exponent_default_and_overrides():
    assert exponent("SAR") == 2 and exponent("usd") == 2
    assert exponent("KWD") == 3 and exponent("BHD") == 3 and exponent("OMR") == 3
    assert exponent("JPY") == 0 and exponent("KRW") == 0
    assert exponent("ZZZ") == 2                      # unknown falls back to 2, never crashes


def test_to_minor_two_decimal():
    assert to_minor(Decimal("150.00"), "SAR") == 15000
    assert to_minor("0.01", "USD") == 1
    assert to_minor(150.1, "SAR") == 15010          # float tail does not leak in


def test_to_minor_three_decimal_kwd():
    assert to_minor("1.234", "KWD") == 1234
    assert to_minor(Decimal("10.5"), "BHD") == 10500


def test_to_minor_zero_decimal_jpy():
    assert to_minor("1500", "JPY") == 1500
    assert to_minor(Decimal("990"), "KRW") == 990


def test_to_major_round_trips():
    assert to_major(15000, "SAR") == Decimal("150.00")
    assert to_major(1234, "KWD") == Decimal("1.234")
    assert to_major(1500, "JPY") == Decimal("1500")


def test_supported_set():
    assert {"SAR", "USD", "EUR"} <= SUPPORTED_CURRENCIES
    assert "XYZ" not in SUPPORTED_CURRENCIES
