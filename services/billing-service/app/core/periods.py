"""Usage period keys — always UTC, always derived.

Usage is stored under a key that CONTAINS its period:

    usage:{tenant}:{key}:2026-07          # a month counter
    usage:{tenant}:{key}:_                # a gauge (no period)

So a new period is simply a new key. **There is no rollover job.** That is deliberate: a reset
cron that fails to run hands every tenant a free month, silently, and you find out from the
revenue. Nothing to miss means nothing to monitor.

`billing_period` anchors to the SUBSCRIPTION, not the calendar: "monthly" means the customer's
month. Someone who subscribed on the 31st has no 31st in February — the anchor clamps to the
last day of the shorter month, the way every payment processor does it, rather than rolling
into March and skipping a period.
"""
from __future__ import annotations

import datetime as _dt

GAUGE_KEY = "_"          # gauges have no period


def _utc(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc)


def _clamp_day(year: int, month: int, day: int) -> int:
    """The 31st of February is the 28th (or 29th). Never March 3rd."""
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    last = (nxt - _dt.timedelta(days=1)).day
    return min(day, last)


def _add_months(d: _dt.date, months: int, anchor_day: int) -> _dt.date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return _dt.date(year, month, _clamp_day(year, month, anchor_day))


def billing_period_key(now: float, anchor: float) -> str:
    """The key of the billing period containing `now`, for a subscription anchored at `anchor`.

    `anchor` is the subscription's start (epoch seconds). Periods run [start, next_start).
    """
    a = _utc(anchor)
    n = _utc(now)
    anchor_day = a.day

    # Months elapsed, then correct by one if we landed after/before the true boundary.
    months = (n.year - a.year) * 12 + (n.month - a.month)
    start_d = _add_months(a.date(), months, anchor_day)
    start = _dt.datetime.combine(start_d, a.timetz())
    if start > n:
        months -= 1
    elif _dt.datetime.combine(_add_months(a.date(), months + 1, anchor_day), a.timetz()) <= n:
        months += 1

    period_start = _dt.datetime.combine(_add_months(a.date(), months, anchor_day), a.timetz())
    return f"bp:{period_start.strftime('%Y-%m-%d')}"


def period_key(period: str, now: float, anchor: float | None = None) -> str:
    """A stable, sortable UTC key for the period containing `now`."""
    if not period:
        return GAUGE_KEY
    n = _utc(now)
    if period == "day":
        return n.strftime("%Y-%m-%d")
    if period == "week":
        # ISO week: the year of the week, not of the date (a 31 Dec can be week 1 of next year).
        iso = n.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "month":
        return n.strftime("%Y-%m")
    if period == "billing_period":
        if anchor is None:
            return n.strftime("%Y-%m")            # no subscription anchor → calendar month
        return billing_period_key(now, anchor)
    raise ValueError(f"unknown period: {period}")
