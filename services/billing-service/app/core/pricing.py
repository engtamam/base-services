"""Pricing engine (pure) — flat / per-seat / usage / hybrid + coupons + proration."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core import coupons as _coupons


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


def coupon_discount(coupon: dict | None, subtotal_minor: int, *, now: float | None = None) -> int:
    """Discount (minor units) — never more than the subtotal.

    `now` gates the campaign window. Without it this checked `active` and nothing else, so a
    coupon that expired in March kept discounting in July: the expiry was stored, displayed, and
    never consulted. Callers that have a clock pass it; the default keeps the old behaviour for a
    caller that genuinely has no time to reason about.
    """
    if not coupon or not coupon.get("active", True):
        return 0
    if now is not None and not _coupons.usable(coupon, now):
        return 0
    ctype, value = coupon.get("type", "percent"), int(coupon.get("value", 0))
    if ctype == "percent":
        disc = int((Decimal(subtotal_minor) * Decimal(value) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    elif ctype == "fixed":
        disc = value
    else:                                    # trial coupon → no monetary discount
        disc = 0
    return max(0, min(disc, subtotal_minor))


def best_rule_percent(rules: list[dict], *, held_keys: set[str], target_keys: set[str],
                      now: float | None = None) -> tuple[int, str]:
    """The single best cross-app discount percent that applies to a subscription, and the rule id.

    A rule applies when the tenant HOLDS its owning_key, the charged subscription carries its
    target_key, it is active, and (if a window is set) `now` is inside it. COMPOSITION IS MAX, by an
    explicit product decision (2026-09-11): when several rules qualify, the HIGHEST single percent
    wins — never summed — so stacked rules can never silently reach 100% off. Returns (0, "") when
    none apply. Pure: the caller supplies the held set and the target set.
    """
    best_pct, best_id = 0, ""
    for r in rules:
        if not r.get("active", True):
            continue
        if r.get("owning_key") not in held_keys or r.get("target_key") not in target_keys:
            continue
        if now is not None:
            starts, ends = r.get("starts_at"), r.get("expires_at")
            if starts is not None and now < float(starts):
                continue
            if ends is not None and now >= float(ends):
                continue
        pct = max(0, min(100, int(r.get("percent", 0) or 0)))
        if pct > best_pct:
            best_pct, best_id = pct, str(r.get("id", ""))
    return best_pct, best_id


def rule_discount(rules: list[dict], subtotal_minor: int, *, held_keys: set[str],
                  target_keys: set[str], now: float | None = None) -> tuple[int, str]:
    """The cross-app discount in MINOR units (best applicable rule), and the rule id. Capped at the
    subtotal — a 100% rule is fully free, never negative. Returns (0, "") when no rule applies."""
    pct, rule_id = best_rule_percent(rules, held_keys=held_keys, target_keys=target_keys, now=now)
    if pct <= 0:
        return 0, ""
    disc = int((Decimal(subtotal_minor) * Decimal(pct) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    return max(0, min(disc, subtotal_minor)), rule_id


def proration_delta(old_minor: int, new_minor: int, *, period_start: float,
                    period_end: float, now: float) -> int:
    """Net charge (minor units) for switching plans mid-period: prorated new − credit old."""
    span = max(1.0, period_end - period_start)
    frac = max(0.0, min(1.0, (period_end - now) / span))    # fraction of the period remaining
    credit = int(Decimal(old_minor) * Decimal(frac))
    charge = int(Decimal(new_minor) * Decimal(frac))
    return charge - credit


def recurring_component(price: dict, *, quantity: int = 1) -> int:
    """The RECURRING commitment portion of a price (minor units) — the part a partial first period
    prorates. Usage is metered on ACTUALS and is NEVER prorated, so:

        flat / per_seat  -> the whole amount is the recurring commitment
        hybrid           -> only the base fee; the overage is metered usage
        usage            -> zero; there is no recurring commitment to prorate

    This is the complement line_amount() splits on: line_amount(...) − recurring_component(...) is
    exactly the metered portion for a period, which a partial first period charges in full.
    """
    kind = price.get("type", "flat")
    base = int(price.get("amount_minor", 0))
    if kind == "flat":
        return base
    if kind == "per_seat":
        return base * max(1, int(quantity))
    if kind == "hybrid":
        return base
    return 0                                  # usage: nothing recurring to prorate


def prorate(amount_minor: int, fraction: Decimal) -> int:
    """`amount_minor` scaled by `fraction` of a period, rounded HALF-UP, clamped to [0, amount].

    `fraction` is an EXACT Decimal built from whole-second period lengths by the caller (never a
    binary float), so the same signup produces the same charge to the minor unit on every platform
    and every run — money must be deterministic, not merely close.
    """
    f = fraction
    if f < 0:
        f = Decimal(0)
    elif f > 1:
        f = Decimal(1)
    scaled = (Decimal(int(amount_minor)) * f).quantize(Decimal(1), ROUND_HALF_UP)
    return max(0, min(int(amount_minor), int(scaled)))
