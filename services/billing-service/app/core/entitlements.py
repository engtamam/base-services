"""Entitlement / limit definitions (pure, general) — the plan's "muscle".

A plan carries a `limits` dict whose KEYS are whatever a project defines (seats, api_calls,
storage_mb, feature.export …). Nothing in this file — or anywhere in base-services — knows
those names. Kinds, periods and overage policies come from allowlists; anything unrecognised
fails CLOSED. That is what makes the same code fit any platform.

Two shapes, both supported:

    "sso": true                                   # scalar  (back-compat)
    "seats": 25                                   # scalar  (a static cap = gauge)
    "api_calls": {"kind": "counter", "limit": 50000,
                  "period": "billing_period", "overage": "allow_and_bill"}

The cap convention (avoids the 0-vs-None bug), unchanged:
    -1 or None  → UNLIMITED
    0           → DENY (feature off)
    N           → limit of N

THE KIND MATTERS, and it is declared, never inferred:

  * counter — monotonic within a period, reset by period key (api_calls, scans, emails).
              `consume(+1)` per use; `release(-1)` refunds a failed action.
  * gauge   — the CURRENT value of something owned elsewhere (seats, projects, storage_mb).
              Reported with `set_usage(n)`. NEVER incremented: increment a gauge and it only
              goes up, so deleting a user would never give the seat back.

A scalar limit is a gauge with no period: `seats: 25` keeps meaning exactly what it meant.
"""
from __future__ import annotations

from typing import Any

UNLIMITED = -1

# Allowlists. An unknown value is a typo or an attack; either way it must not silently pass.
KINDS = ("counter", "gauge", "boolean")
PERIODS = ("day", "week", "month", "billing_period")
OVERAGE_POLICIES = (
    "block",           # deny once the limit is reached (the default; safe)
    "allow_and_bill",  # allow, record the excess for the overage invoice
    "soft",            # allow, flag it, bill nothing (a nudge, not a wall)
)

DEFAULT_KIND = "gauge"          # a bare number is a static cap
DEFAULT_PERIOD = "billing_period"
DEFAULT_OVERAGE = "block"


class EntitlementError(ValueError):
    """A malformed entitlement definition. Fails closed at the call site."""


def is_unlimited(limit: Any) -> bool:
    return limit is None or limit == UNLIMITED


def _coerce_limit(value: Any) -> int:
    if is_unlimited(value):
        return UNLIMITED
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0                                   # unparseable → deny, never allow


def spec(limits: dict[str, Any], key: str, *, default: Any = 0,
         default_period: str = DEFAULT_PERIOD) -> dict[str, Any]:
    """Resolve `key` into a normalized spec. Unknown key → the caller's default (deny by default).

    Returns {key, kind, limit, period, overage, unlimited, present}.

    `present` distinguishes "the plan grants 0 of this" from "the plan never mentions it".
    Both deny, but only the second one means the tenant simply has no such entitlement — and
    reporting it as a zero-limit gauge produces nonsense errors downstream.
    """
    present = isinstance(limits, dict) and key in limits
    raw = limits.get(key, default) if isinstance(limits, dict) else default

    if isinstance(raw, dict):
        kind = str(raw.get("kind", DEFAULT_KIND))
        if kind not in KINDS:
            raise EntitlementError(f"UNKNOWN_KIND:{kind}")
        period = str(raw.get("period", default_period))
        overage = str(raw.get("overage", DEFAULT_OVERAGE))
        if kind == "counter":
            if period not in PERIODS:
                raise EntitlementError(f"UNKNOWN_PERIOD:{period}")
        else:
            period = ""                            # gauges and booleans have no period
        if overage not in OVERAGE_POLICIES:
            raise EntitlementError(f"UNKNOWN_OVERAGE:{overage}")
        limit = _coerce_limit(raw.get("limit"))
    else:
        # Scalar back-compat: True/False are booleans; a number is a static cap (a gauge).
        if isinstance(raw, bool):
            kind, limit, period, overage = "boolean", (UNLIMITED if raw else 0), "", DEFAULT_OVERAGE
        else:
            kind, limit, period, overage = DEFAULT_KIND, _coerce_limit(raw), "", DEFAULT_OVERAGE

    return {"key": key, "kind": kind, "limit": limit, "period": period,
            "overage": overage, "unlimited": is_unlimited(limit), "present": present}


def evaluate(sp: dict[str, Any], current: int = 0, amount: int = 1) -> dict[str, Any]:
    """Would consuming `amount` more of this entitlement be allowed, given `current` usage?

    PURE. `current` is supplied by the SERVICE (from its own usage store) — never by the
    caller of the HTTP API. A client that can state its own usage has no limit at all.
    """
    if sp["unlimited"]:
        return {"allowed": True, "unlimited": True, "limit": None, "remaining": None,
                "current": int(current), "over_limit": False, "overage": 0,
                "key": sp["key"], "kind": sp["kind"], "policy": sp["overage"]}

    limit = int(sp["limit"])
    cur = max(0, int(current))
    amt = int(amount)
    projected = cur + amt

    within = projected <= limit
    # `over_limit` describes the CURRENT state (e.g. after a downgrade: 40 seats used, plan
    # now allows 25). It is not the same as "this request exceeds the limit".
    over_limit = cur > limit
    # Only a counter can run an overage. A denied boolean reporting `overage: 1` would be
    # meaningless, and billing must never see a unit it cannot price.
    overage_units = 0 if (within or not is_metered(sp)) else projected - max(limit, cur)

    if within:
        allowed = True
    elif sp["overage"] == "allow_and_bill":
        allowed = True                             # bill the excess, do not block the customer
    elif sp["overage"] == "soft":
        allowed = True                             # warn only
    else:
        allowed = False                            # "block" — the safe default

    return {"allowed": allowed, "unlimited": False, "limit": limit,
            "remaining": max(0, limit - cur), "current": cur,
            "over_limit": over_limit, "overage": max(0, overage_units),
            "key": sp["key"], "kind": sp["kind"], "policy": sp["overage"]}


def check(limits: dict[str, Any], key: str, current: int = 0, *, default: Any = 0,
          amount: int = 1) -> dict[str, Any]:
    """Back-compatible pure check. Fails CLOSED on a malformed definition."""
    try:
        return evaluate(spec(limits, key, default=default), current, amount)
    except EntitlementError as exc:
        return {"allowed": False, "unlimited": False, "limit": 0, "remaining": 0,
                "current": int(current), "over_limit": False, "overage": 0,
                "key": key, "kind": "unknown", "policy": "block", "error": str(exc)}


def feature_enabled(limits: dict[str, Any], key: str) -> bool:
    """A boolean/flag entitlement: unlimited or a positive cap → on; 0/False/absent → off."""
    try:
        sp = spec(limits, key)
    except EntitlementError:
        return False                               # fail closed
    return bool(sp["unlimited"] or int(sp["limit"]) > 0)


def is_metered(sp: dict[str, Any]) -> bool:
    """Counters are consumed; gauges are reported; booleans are neither."""
    return sp["kind"] == "counter"
