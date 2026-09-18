"""ISO-4217 minor-unit conversion — the ONE place money crosses the gateway boundary.

A gateway talks major units (e.g. TuwaiqPay's `amount: 150.00`); we store minor units (integer
`amount_minor`). The exponent is currency-dependent — most are 2 (SAR/USD/EUR), but KWD/BHD/OMR/JOD/TND
are 3 and JPY/KRW are 0 — so a hardcoded `/100` silently mischarges a 3-dp or 0-dp currency by 10x.
Math is Decimal/integer only: `float(0.1*3)` is not `0.3`, and money must not round wrong.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_DEFAULT_EXPONENT = 2
# Only the non-2 exponents need listing; everything else falls back to _DEFAULT_EXPONENT.
_EXPONENTS: dict[str, int] = {
    "KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "TND": 3,
    "JPY": 0, "KRW": 0,
}

# Currencies this conduit can actually bill — the gateway-supported ISO set (TuwaiqPay's currencyId
# map is SAR/USD/EUR). create_link validates against this so an unknown currency is a clear 422,
# never silently coerced to SAR. Widen this as more gateways/currencies are onboarded.
SUPPORTED_CURRENCIES = frozenset({"SAR", "USD", "EUR"})


def exponent(currency: str) -> int:
    """Minor-unit exponent for an ISO-4217 code (2 by default)."""
    return _EXPONENTS.get((currency or "").upper(), _DEFAULT_EXPONENT)


def to_minor(amount_major, currency: str) -> int:
    """Major units → integer minor units (e.g. SAR 150.00 → 15000, KWD 1.234 → 1234)."""
    scale = Decimal(10) ** exponent(currency)
    # Decimal(str(x)) so a float like 150.1 does not smuggle in its binary-repr tail.
    return int((Decimal(str(amount_major)) * scale).to_integral_value(rounding=ROUND_HALF_UP))


def to_major(amount_minor: int, currency: str) -> Decimal:
    """Integer minor units → exact major-unit Decimal (e.g. 15000 SAR → 150.00, 1234 KWD → 1.234)."""
    scale = Decimal(10) ** exponent(currency)
    return (Decimal(int(amount_minor)) / scale).quantize(Decimal(1) / scale)
