"""Usage metering + entitlement enforcement (Phase 25).

The defects this file exists to prevent, each of which shipped once:

  * the caller supplied its own usage number, so every limit passed;
  * nothing counted usage, so counted limits were decorative;
  * check-then-act let two concurrent requests both take the last unit;
  * a gauge (seats) incremented like a counter never gives the seat back;
  * a redelivered event burned a second unit of quota;
  * a period-reset cron that fails hands every tenant a free month.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import entitlements as ent
from app.core import periods
from app.core.usage_service import UsageService

ANCHOR = dt.datetime(2026, 1, 31, 10, tzinfo=dt.timezone.utc).timestamp()   # subscribed the 31st
FEB = dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc).timestamp()


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


class Meter:
    """Stands in for UsageStore — what run_overage() later aggregates and invoices."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, int]] = []

    async def record(self, tenant, sub_id, metric, quantity):
        self.rows.append((metric, int(quantity)))


LIMITS = {
    "sso": True,
    "export": False,
    "seats": 25,                                                    # scalar => gauge
    "api_calls": {"kind": "counter", "limit": 10, "period": "billing_period", "overage": "block"},
    "emails": {"kind": "counter", "limit": 5, "period": "month", "overage": "allow_and_bill"},
    "reports": {"kind": "counter", "limit": 3, "period": "day", "overage": "soft"},
    "storage_mb": {"kind": "gauge", "limit": -1},                   # unlimited
}


def _svc(now=FEB, limits=None, store=None, meter=None):
    async def resolver(_tenant):
        return {"limits": LIMITS if limits is None else limits,
                "anchor": ANCHOR, "subscription_id": "sub_1"}

    return UsageService(store=store or MemoryDataStore(), limits_resolver=resolver,
                        clock=Clock(now), meter=meter,
                        config={"default_period": "billing_period", "idempotency_ttl": 3600,
                                "unknown_key_default": 0})


# ── the pure layer: kinds/periods/policies are data, unknown values fail closed ──
def test_unknown_kind_period_or_policy_fails_closed():
    for bad in ({"kind": "magic", "limit": 5},
                {"kind": "counter", "limit": 5, "period": "fortnight"},
                {"kind": "counter", "limit": 5, "overage": "just_allow_it"}):
        with pytest.raises(ent.EntitlementError):
            ent.spec({"k": bad}, "k")
        assert ent.check({"k": bad}, "k")["allowed"] is False       # the wrapper fails closed


def test_scalar_limits_keep_their_old_meaning():
    assert ent.spec({"seats": 25}, "seats")["kind"] == "gauge"
    assert ent.spec({"sso": True}, "sso")["kind"] == "boolean"
    assert ent.spec({"x": -1}, "x")["unlimited"] is True
    assert ent.check({"export": 0}, "export")["allowed"] is False   # 0 = deny
    assert ent.check({}, "unknown_key")["allowed"] is False         # absent = deny


def test_a_key_absent_from_the_plan_is_denied_not_allowed():
    assert ent.feature_enabled({}, "anything") is False
    assert ent.feature_enabled({"sso": True}, "sso") is True


# ── periods: no rollover cron, and February has no 31st ─────────────────────
def test_billing_period_clamps_a_31st_anchor_into_february():
    def key(y, m, d):
        return periods.period_key("billing_period",
                                  dt.datetime(y, m, d, 12, tzinfo=dt.timezone.utc).timestamp(),
                                  ANCHOR)

    assert key(2026, 2, 27) == "bp:2026-01-31"     # still in the January period
    assert key(2026, 2, 28) == "bp:2026-02-28"     # clamped, NOT skipped to March
    assert key(2026, 3, 30) == "bp:2026-02-28"
    assert key(2026, 3, 31) == "bp:2026-03-31"


def test_billing_periods_are_contiguous_and_monotonic():
    start = dt.datetime(2026, 1, 31, 12, tzinfo=dt.timezone.utc)
    seen, prev = set(), ""
    for i in range(370):
        k = periods.period_key("billing_period", (start + dt.timedelta(days=i)).timestamp(), ANCHOR)
        assert k >= prev                                            # sortable
        prev = k
        seen.add(k)
    assert 11 <= len(seen) <= 14                                    # ~one per month, no gaps


def test_calendar_periods():
    t = dt.datetime(2026, 7, 10, tzinfo=dt.timezone.utc).timestamp()
    assert periods.period_key("day", t) == "2026-07-10"
    assert periods.period_key("month", t) == "2026-07"
    assert periods.period_key("", t) == periods.GAUGE_KEY            # gauges have no period


def test_a_new_period_is_a_new_key_so_no_reset_job_can_be_missed():
    svc = _svc()
    for _ in range(10):
        run(svc.consume(tenant="t", key="api_calls"))
    assert run(svc.current("t", "api_calls")) == 10

    svc._clock.t = dt.datetime(2026, 3, 5, tzinfo=dt.timezone.utc).timestamp()
    assert run(svc.current("t", "api_calls")) == 0                   # nothing had to run


# ── the footgun: check-then-act ─────────────────────────────────────────────
def test_fifty_concurrent_consumes_never_exceed_the_limit():
    svc = _svc()

    async def race():
        return await asyncio.gather(*[svc.consume(tenant="t", key="api_calls") for _ in range(50)])

    results = asyncio.run(race())
    allowed = sum(r["allowed"] for r in results)
    assert allowed == 10, f"{allowed} requests won a limit of 10 — check-then-act race"
    assert run(svc.current("t", "api_calls")) == 10


def test_the_store_itself_refuses_to_exceed_the_bound():
    st = MemoryDataStore()

    async def go():
        res = await asyncio.gather(*[st.incr_if_below("c", "k", 1, 25) for _ in range(50)])
        return sum(1 for ok, _ in res if ok), (await st.get("c", "k"))["n"]

    allowed, final = asyncio.run(go())
    assert (allowed, final) == (25, 25)


def test_a_release_is_never_refused_and_clamps_at_zero():
    st = MemoryDataStore()

    async def go():
        await st.incr_if_below("c", "k", 5, 10)
        assert await st.incr_if_below("c", "k", -3, 10) == (True, 2)
        assert await st.incr_if_below("c", "k", -99, 10) == (True, 0)   # clamped
        await st.incr_if_below("c", "k", 40, None)                      # now over any limit
        assert await st.incr_if_below("c", "k", -1, 10) == (True, 39)   # refund still applies

    asyncio.run(go())


# ── enforcement semantics ───────────────────────────────────────────────────
def test_block_policy_denies_past_the_limit():
    svc = _svc()
    outs = [run(svc.consume(tenant="t", key="api_calls")) for _ in range(12)]
    assert sum(o["allowed"] for o in outs) == 10
    assert outs[-1]["remaining"] == 0
    assert run(svc.current("t", "api_calls")) == 10          # denied attempts wrote nothing


def test_allow_and_bill_lets_the_customer_through_and_meters_the_excess():
    meter = Meter()
    svc = _svc(meter=meter)
    outs = [run(svc.consume(tenant="t", key="emails")) for _ in range(7)]
    assert all(o["allowed"] for o in outs), "allow_and_bill must never block"
    assert sum(q for _, q in meter.rows) == 2                 # units 6 and 7
    assert {m for m, _ in meter.rows} == {"emails"}


def test_soft_policy_allows_and_flags_but_bills_nothing():
    meter = Meter()
    svc = _svc(meter=meter)
    outs = [run(svc.consume(tenant="t", key="reports")) for _ in range(5)]
    assert all(o["allowed"] for o in outs)
    assert outs[-1]["overage"] > 0
    assert meter.rows == [], "soft must not bill"


def test_a_redelivered_consume_charges_exactly_once():
    svc = _svc()
    first = run(svc.consume(tenant="t", key="emails", idempotency_key="evt-1"))
    again = run(svc.consume(tenant="t", key="emails", idempotency_key="evt-1"))
    assert first["current"] == 1
    assert again["current"] == 1 and again["idempotent"] is True
    assert run(svc.current("t", "emails")) == 1


def test_a_denied_consume_is_not_memoised():
    """Otherwise a retry after an upgrade would replay the old denial."""
    svc = _svc()
    for i in range(10):
        run(svc.consume(tenant="t", key="api_calls", idempotency_key=f"k{i}"))
    denied = run(svc.consume(tenant="t", key="api_calls", idempotency_key="k-denied"))
    assert denied["allowed"] is False
    svc._store._data.pop("usage_counters")                    # simulate a new period
    retry = run(svc.consume(tenant="t", key="api_calls", idempotency_key="k-denied"))
    assert retry["allowed"] is True and not retry.get("idempotent")


def test_release_returns_a_counter_unit_after_a_failed_action():
    svc = _svc()
    run(svc.consume(tenant="t", key="api_calls"))
    out = run(svc.release(tenant="t", key="api_calls"))
    assert out["current"] == 0


# ── gauge vs counter: THE correctness footgun ───────────────────────────────
def test_a_gauge_cannot_be_consumed():
    svc = _svc()
    out = run(svc.consume(tenant="t", key="seats"))
    assert out["allowed"] is False and out["error"] == "GAUGE_NOT_CONSUMABLE"


def test_a_counter_cannot_be_set():
    svc = _svc()
    assert run(svc.set_usage(tenant="t", key="api_calls", value=3))["error"] == "NOT_A_GAUGE"


def test_deleting_a_user_gives_the_seat_back():
    """A gauge is absolute. Incremented like a counter, seats would only ever grow."""
    svc = _svc()
    run(svc.set_usage(tenant="t", key="seats", value=25))
    assert run(svc.check(tenant="t", key="seats"))["allowed"] is False
    run(svc.set_usage(tenant="t", key="seats", value=24))            # a user was removed
    assert run(svc.check(tenant="t", key="seats"))["allowed"] is True


def test_a_gauge_has_no_period_so_it_never_resets():
    svc = _svc()
    run(svc.set_usage(tenant="t", key="seats", value=7))
    svc._clock.t = dt.datetime(2027, 9, 9, tzinfo=dt.timezone.utc).timestamp()
    assert run(svc.current("t", "seats")) == 7                        # seats don't reset monthly


def test_downgrade_below_current_usage_reports_over_limit_and_blocks_new_consumption():
    svc = _svc()
    run(svc.set_usage(tenant="t", key="seats", value=40))             # plan now allows 25
    out = run(svc.check(tenant="t", key="seats"))
    assert out["over_limit"] is True
    assert out["allowed"] is False          # no NEW seats...
    assert out["current"] == 40             # ...and nobody was deleted to make it fit
    assert out["remaining"] == 0


# ── the API must not accept a usage number from its caller ──────────────────
def test_usage_service_never_takes_current_from_the_caller():
    import inspect

    for name in ("check", "consume", "release"):
        params = inspect.signature(getattr(UsageService, name)).parameters
        assert "current" not in params, (
            f"UsageService.{name} accepts `current` — a caller that states its own usage "
            f"has no limit at all (CWE-602)"
        )


def test_http_entitlement_routes_expose_no_current_parameter():
    import inspect

    from app.api import internal
    from app.api.v1 import billing as billing_routes

    assert "current" not in inspect.signature(billing_routes.entitlement).parameters
    assert "current" not in inspect.signature(internal.entitlement_check).parameters


def test_boolean_entitlements_still_answer_the_way_feature_flag_expects():
    """feature-flag-service reads only `allowed`; its behaviour must not change."""
    svc = _svc()
    assert run(svc.check(tenant="t", key="sso"))["allowed"] is True
    assert run(svc.check(tenant="t", key="export"))["allowed"] is False
    assert run(svc.check(tenant="t", key="nope"))["allowed"] is False


def test_unlimited_is_never_metered_into_a_denial():
    svc = _svc()
    out = run(svc.check(tenant="t", key="storage_mb"))
    assert out["allowed"] is True and out["unlimited"] is True


def test_summary_reports_remaining_so_clients_never_count_for_themselves():
    svc = _svc()
    run(svc.set_usage(tenant="t", key="seats", value=18))
    summary = run(svc.summary("t"))["entitlements"]
    assert summary["seats"]["current"] == 18
    assert summary["seats"]["remaining"] == 7
    assert summary["sso"]["allowed"] is True


# ── "absent" is not "a zero-limit gauge" ────────────────────────────────────
def test_an_absent_key_is_reported_as_not_entitled_not_as_a_gauge():
    svc = _svc(limits={})
    out = run(svc.consume(tenant="t", key="api_calls"))
    assert out["allowed"] is False
    assert out["error"] == "NOT_ENTITLED"          # not "GAUGE_NOT_CONSUMABLE"


def test_present_distinguishes_granted_zero_from_never_mentioned():
    assert ent.spec({"export": 0}, "export")["present"] is True
    assert ent.spec({}, "export")["present"] is False


def test_a_denied_boolean_reports_no_overage():
    """Billing must never be handed a unit it cannot price."""
    out = ent.check({"sso": False}, "sso")
    assert out["allowed"] is False and out["overage"] == 0


def test_only_counters_report_overage():
    assert ent.check({"seats": 3}, "seats", 3)["overage"] == 0        # gauge
    over = ent.check({"c": {"kind": "counter", "limit": 3, "overage": "soft"}}, "c", 3)
    assert over["overage"] == 1                                       # counter
