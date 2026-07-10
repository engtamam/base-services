"""Pricing engine (pure) — flat / per-seat / usage / hybrid + coupons + proration."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def line_amount(price: dict, *, quantity: int = 1, usage_units: int = 0) -> int:
    """Subtotal (minor units) for one price under a subscription."""
    kind = price.get("type", "flat")
    base = int(price.get("amount_minor", 0))
    if kind == "flat":
        return base
    if kind == "per_seat":
        return base * max(1, int(quantity))
    if kind == "usage":
        return int(price.get("overage_minor", 0)) * max(0, int(usage_units))
    if kind == "hybrid":
        over = max(0, int(usage_units) - int(price.get("included_units", 0)))
        return base + int(price.get("overage_minor", 0)) * over
    return base


def coupon_discount(coupon: dict | None, subtotal_minor: int) -> int:
    """Discount (minor units) — never more than the subtotal."""
    if not coupon or not coupon.get("active", True):
        return 0
    ctype, value = coupon.get("type", "percent"), int(coupon.get("value", 0))
    if ctype == "percent":
        disc = int((Decimal(subtotal_minor) * Decimal(value) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    elif ctype == "fixed":
        disc = value
    else:                                    # trial coupon → no monetary discount
        disc = 0
    return max(0, min(disc, subtotal_minor))


def proration_delta(old_minor: int, new_minor: int, *, period_start: float,
                    period_end: float, now: float) -> int:
    """Net charge (minor units) for switching plans mid-period: prorated new − credit old."""
    span = max(1.0, period_end - period_start)
    frac = max(0.0, min(1.0, (period_end - now) / span))    # fraction of the period remaining
    credit = int(Decimal(old_minor) * Decimal(frac))
    charge = int(Decimal(new_minor) * Decimal(frac))
    return charge - credit
