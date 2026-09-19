"""ISO-4217 minor<->major must honour each currency's real exponent (safety:money-float / L6).

A hardcoded /100 mischarges a 3-dp (KWD/BHD) or 0-dp (JPY) currency by 10-100x. The TuwaiqPay
adapter now routes through this helper (see app/adapters/payment/tuwaiqpay.py) instead of a bare /100.
"""
from __future__ import annotations

from app.core.currency import major, to_minor


def test_major_honours_the_currency_exponent():
    assert major(15000, "SAR") == "150.00"      # 2-dp
    assert major(1234, "KWD") == "1.234"        # 3-dp — a /100 would have said 12.34
    assert major(500, "JPY") == "500"           # 0-dp — a /100 would have said 5.00


def test_to_minor_is_the_exact_inverse():
    assert to_minor("150.00", "SAR") == 15000
    assert to_minor("1.234", "KWD") == 1234     # a *100 would have said 123
    assert to_minor("500", "JPY") == 500
    assert to_minor(12.30, "SAR") == 1230       # a float major does not smuggle its binary tail


def test_round_trips_across_exponents():
    for cur, minor in (("SAR", 19999), ("KWD", 1234567), ("JPY", 42), ("BHD", 500)):
        assert to_minor(major(minor, cur), cur) == minor
