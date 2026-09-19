"""Currency-aware minor units — most currencies are 2-dp; KWD/BHD/OMR/etc. are 3-dp.

General: any project sets prices in minor units; this converts correctly for the
gateway's expected format (major decimal vs minor integer) without hardcoding SAR.
"""
from __future__ import annotations

from decimal import Decimal

# ISO-4217 exponents that are NOT 2. (Everything else defaults to 2.)
_DECIMALS = {"KWD": 3, "BHD": 3, "OMR": 3, "TND": 3, "JOD": 3, "IQD": 3,
             "JPY": 0, "KRW": 0, "CLP": 0, "VND": 0}


def decimals(currency: str) -> int:
    return _DECIMALS.get((currency or "").upper(), 2)


def minor_per(currency: str) -> int:
    return 10 ** decimals(currency)


def major(amount_minor: int, currency: str = "SAR") -> str:
    """Minor units → major decimal string with the currency's real precision."""
    dp = decimals(currency)
    return f"{Decimal(int(amount_minor)) / (10 ** dp):.{dp}f}"


def to_minor(amount_major, currency: str = "SAR") -> int:
    """Major units → integer minor units at the currency's real precision (the inverse of major()).
    Decimal(str(x)) so a float like 12.30 does not smuggle in its binary-repr tail; ROUND_HALF_UP so
    a gateway-reported major amount lands on the exact integer minor unit."""
    from decimal import ROUND_HALF_UP
    scale = Decimal(10) ** decimals(currency)
    return int((Decimal(str(amount_major)) * scale).to_integral_value(rounding=ROUND_HALF_UP))
