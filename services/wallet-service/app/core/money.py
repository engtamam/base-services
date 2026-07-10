"""Money — integer MINOR UNITS only (halalas, cents, fils). Never a float.

`0.1 + 0.2 != 0.3`. A wallet that stores money as a float loses a halala per operation and
cannot be reconciled against its own ledger. Every amount in this service is an `int` of the
currency's smallest unit, and the only place a decimal appears is when a UI formats one.

VAT is quoted in BASIS POINTS (1500 = 15%) so the rate itself is an integer too.

Rounding is stated once, here, and applied nowhere else: VAT rounds half-up to the nearest
minor unit. Two different rounding rules in one codebase produce invoices that do not add up.
"""
from __future__ import annotations


class MoneyError(ValueError):
    pass


def require_non_negative(amount: int, what: str = "amount") -> int:
    a = int(amount)
    if a < 0:
        raise MoneyError(f"{what.upper()}_MUST_NOT_BE_NEGATIVE")
    return a


def vat_of(net_minor: int, vat_bp: int) -> int:
    """VAT on a net amount, rounded HALF-UP to the nearest minor unit."""
    net = require_non_negative(net_minor, "net")
    bp = require_non_negative(vat_bp, "vat_bp")
    # (net * bp + 5000) // 10000 == round-half-up(net * bp / 10000), in pure integers.
    return (net * bp + 5_000) // 10_000


def gross_of(net_minor: int, vat_bp: int) -> tuple[int, int, int]:
    """(net, vat, gross) — all integers, gross == net + vat exactly."""
    net = require_non_negative(net_minor, "net")
    vat = vat_of(net, vat_bp)
    return net, vat, net + vat
