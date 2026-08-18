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


def aligned_period_end(now: float, interval_seconds: float, reference_end: float) -> float:
    """The first period-end for a NEW subscription so it shares an EXISTING one's billing grid.

    A tenant's second subscription must not open its own billing cycle on its own day — a customer
    who buys on the 3rd and again on the 17th would be charged twice a month on two days for ever.
    Every subscription advances by the same fixed `interval_seconds` (see billing_service._INTERVAL),
    so two subscriptions stay aligned for ever iff their period boundaries are congruent modulo that
    interval. This returns the smallest grid boundary STRICTLY AFTER `now` on the reference's grid —
    i.e. `reference_end + k*interval` for the least integer k with the result > now.

    The new subscription's first period [now, aligned_end) is therefore PARTIAL, and its recurring
    charge is prorated by that fraction (billing_service.run_cycle); every cycle after advances a full
    interval and stays on the shared grid. Returns `now` for a non-renewing interval (caller must not
    align those). Pure and integer-second deterministic — the same inputs give the same boundary on
    every platform.
    """
    if interval_seconds <= 0:
        return now                                       # non-renewing; nothing to align to
    # Least integer k such that reference_end + k*interval > now. floor(x)+1 is strictly-greater even
    # when now sits exactly on a boundary, so the first period is never zero-or-negative length.
    import math
    k = math.floor((now - reference_end) / interval_seconds) + 1
    return reference_end + k * interval_seconds


def distinct_renewing_period_ends(subs: list[dict], interval: str) -> set[int]:
    """The set of distinct current_period_end values (whole seconds) among a tenant's LIVE, RENEWING
    subscriptions of one interval — the invariant behind alignment. A tenant past its subscriptions'
    first cycle must have exactly ONE, per interval: they share a billing grid, so they renew on ONE
    day. Excludes non-renewing ("once", zero end) and trial periods (a trial end is not a grid
    boundary). Any product on the platform can assert this to catch a drift the moment it appears.
    """
    ends: set[int] = set()
    for s in subs:
        if s.get("interval") != interval or interval == "once":
            continue
        if s.get("status") == "trialing":
            continue
        end = float(s.get("current_period_end") or 0)
        if end > 0:
            ends.add(round(end))
    return ends


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
