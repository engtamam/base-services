"""A tenant's entitlements are the union of everything they pay for.

Resolution used to read ONE subscription — `limit=1, sort=created desc` — so a customer who bought
three things was entitled to one, and WHICH one depended on insertion order. The refusal was a
fail-closed 402, indistinguishable from "you have not paid for this", so everyone involved read it
as a billing problem with the customer.

This file is ordered by what must never happen:

    1. a paying customer refused          — including every EXISTING single-subscription tenant,
                                            which must resolve byte-identically to before
    2. a non-paying tenant granted        — cancelled, or long past due
    3. a silent answer                    — no provenance, or a key dropped with no reason
    4. a slow answer                      — the hot path must not become N reads
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.entitlement_merge import MergeRefused, UNLIMITED, merge
from app.core.entitlement_resolver import EntitlementResolver
from app.core.models import Plan
from app.core.stores import PlanStore, SubscriptionStore

NOW = 1_700_000_000.0
DAY = 86400.0

_CFG = {"billable_statuses": ("active", "trialing", "past_due"), "grace_days": 7,
        "grandfather": True, "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
        "period_tolerance_seconds": 86400}


def run(c):
    return asyncio.run(c)


def _world(**over):
    store = MemoryDataStore()
    cfg = {**_CFG, **over}
    return store, EntitlementResolver(subs=SubscriptionStore(store), plans=PlanStore(store),
                                      cfg=cfg)


def _sub(store, sub_id, *, limits=None, status="active", created="2024-01-01",
         period_end=NOW + DAY, plan_id="", stacking=None, tenant="acme", quantity=1):
    row = {"id": sub_id, "tenant": tenant, "status": status, "created": created,
           "plan_id": plan_id, "limits_snapshot": limits or {}, "quantity": quantity,
           "current_period_start": NOW - DAY, "current_period_end": period_end}
    if stacking:
        row["stacking"] = stacking
    run(store.put("subscriptions", sub_id, row))
    return row


# ── 1. a paying customer must not be refused ──────────────────────────────────
def test_a_tenant_with_THREE_subscriptions_gets_all_three():
    """THE bug. One purchase used to win and the other two vanished."""
    store, r = _world()
    _sub(store, "sub_a", limits={"standard:ecc": True}, created="2024-01-01")
    _sub(store, "sub_b", limits={"standard:sama": True}, created="2024-01-02")
    _sub(store, "sub_c", limits={"platform": True}, created="2024-01-03")

    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {"standard:ecc": True, "standard:sama": True, "platform": True}


def test_seat_packs_SUM():
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 20}, created="2024-01-01")
    _sub(store, "sub_b", limits={"seats": 5}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 25


# ── quantity scales a row's OWN numeric limits (one seat/branch pack row of quantity N) ────────────
def test_quantity_scales_a_single_rows_numeric_limit():
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": 10}, quantity=3)   # one pack row, quantity 3
    assert run(r.resolve("acme", NOW))["limits"]["staff.seats"] == 30   # 10 × 3, not 10


def test_quantity_scales_per_row_then_sums():
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": 10}, quantity=3, created="2024-01-01")  # 30
    _sub(store, "sub_b", limits={"staff.seats": 5}, quantity=1, created="2024-01-02")   # 5
    assert run(r.resolve("acme", NOW))["limits"]["staff.seats"] == 35   # multiply-then-sum


def test_quantity_never_multiplies_UNLIMITED():
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": UNLIMITED}, quantity=5)
    assert run(r.resolve("acme", NOW))["limits"]["staff.seats"] == UNLIMITED   # stays unlimited


def test_quantity_never_multiplies_a_boolean_flag():
    store, r = _world()
    _sub(store, "sub_a", limits={"sso": True}, quantity=4)
    assert run(r.resolve("acme", NOW))["limits"]["sso"] is True   # a flag is not a quantity


def test_quantity_scales_a_rich_gauge_inner_limit():
    # A real per-seat plan stores its limit as a rich dict; quantity must scale the inner `limit`,
    # not be ignored (audit: a gauge-shaped add-on was unsellable at quantity > 1).
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": {"kind": "gauge", "limit": 1}}, quantity=5)
    got = run(r.resolve("acme", NOW))["limits"]["staff.seats"]
    assert got["limit"] == 5 and got["kind"] == "gauge"   # inner scaled, shape preserved


def test_quantity_scales_a_rich_counter_and_preserves_period():
    store, r = _world()
    _sub(store, "sub_a",
         limits={"api.calls": {"kind": "counter", "limit": 1000, "period": "billing_period"}},
         quantity=3)
    got = run(r.resolve("acme", NOW))["limits"]["api.calls"]
    assert got["limit"] == 3000 and got["kind"] == "counter" and got["period"] == "billing_period"


def test_quantity_never_scales_a_rich_unlimited_inner():
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": {"kind": "gauge", "limit": UNLIMITED}}, quantity=5)
    got = run(r.resolve("acme", NOW))["limits"]["staff.seats"]
    assert got["limit"] == UNLIMITED           # a non-numeric inner limit is never multiplied


def test_rich_gauge_scales_per_row_then_merges():
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": {"kind": "gauge", "limit": 2}},
         quantity=5, created="2024-01-01")     # 10
    _sub(store, "sub_b", limits={"staff.seats": {"kind": "gauge", "limit": 1}},
         quantity=3, created="2024-01-02")     # 3
    assert run(r.resolve("acme", NOW))["limits"]["staff.seats"]["limit"] == 13   # 10 + 3


def test_quantity_one_is_an_exact_noop():
    # every EXISTING row defaults to quantity 1 — resolution must be byte-identical to pre-change.
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": 10, "sso": True}, quantity=1)
    out = run(r.resolve("acme", NOW))["limits"]
    assert out["staff.seats"] == 10 and out["sso"] is True


def test_a_bad_quantity_never_zeroes_a_grant():
    # a missing / 0 quantity coerces to a no-op, never 0 seats (which would lock a customer out).
    store, r = _world()
    _sub(store, "sub_a", limits={"staff.seats": 10}, quantity=0)
    assert run(r.resolve("acme", NOW))["limits"]["staff.seats"] == 10


def test_a_boolean_is_granted_by_ANY_subscription():
    store, r = _world()
    _sub(store, "sub_a", limits={"sso": False}, created="2024-01-01")
    _sub(store, "sub_b", limits={"sso": True}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["sso"] is True


def test_a_declared_MAX_key_takes_the_larger_not_the_sum():
    """Adding retention windows is meaningless; the longer plan wins."""
    store, r = _world(merge_strategies={"retention_days": "max"})
    _sub(store, "sub_a", limits={"retention_days": 30}, created="2024-01-01")
    _sub(store, "sub_b", limits={"retention_days": 90}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["retention_days"] == 90


def test_RANK_takes_the_highest_which_is_neither_sum_nor_max():
    """The key that proves a global sum-or-max switch cannot work."""
    store, r = _world(merge_strategies={"support_tier": "rank"},
                      rank_orders={"support_tier": ["bronze", "silver", "gold"]})
    _sub(store, "sub_a", limits={"support_tier": "gold"}, created="2024-01-01")
    _sub(store, "sub_b", limits={"support_tier": "silver"}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["support_tier"] == "gold"


def test_UNLIMITED_beats_arithmetic():
    """Summing 'unlimited' with a number, or comparing it as a string, produces a number SMALLER
    than unlimited — capping a customer who bought no cap at all."""
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 10}, created="2024-01-01")
    _sub(store, "sub_b", limits={"seats": UNLIMITED}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == UNLIMITED


def test_a_past_due_subscription_INSIDE_grace_still_counts():
    """A late bank transfer must not switch off a hospital's cameras."""
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 10}, status="past_due", period_end=NOW - DAY)
    assert run(r.resolve("acme", NOW))["limits"] == {"seats": 10}


# ── 1b. EXISTING tenants must resolve identically ─────────────────────────────
def test_a_single_subscription_resolves_exactly_as_before():
    """42.11, and the reason this phase is the riskiest thing we have touched. Every tenant with
    one subscription must see what they saw yesterday."""
    store, r = _world()
    _sub(store, "sub_only", limits={"seats": 25, "sso": True})

    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {"seats": 25, "sso": True}
    assert out["subscription_id"] == "sub_only"
    assert out["anchor"] == NOW - DAY


def test_a_tenant_with_NO_subscription_resolves_empty_not_an_error():
    store, r = _world()
    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {} and out["subscription_id"] == "" and out["anchor"] is None


def test_grandfathering_is_PER_SUBSCRIPTION_before_the_merge():
    """One row's signup terms must not leak onto another bought yesterday."""
    store, r = _world()
    run(store.put("plans", "plan_new", Plan(id="plan_new", name="new", prices=[],
                                            limits={"seats": 999}).to_dict()))
    _sub(store, "sub_old", limits={"seats": 5}, created="2024-01-01")          # snapshot wins
    _sub(store, "sub_new", limits={}, plan_id="plan_new", created="2024-01-02")  # catalogue

    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 1004


# ── 2. a non-paying tenant must not be granted ────────────────────────────────
def test_a_CANCELLED_subscription_contributes_nothing():
    """Not configurable. A cancelled subscription that still granted access would make
    cancellation a suggestion."""
    store, r = _world(billable_statuses=("active", "trialing", "past_due", "canceled"))
    _sub(store, "sub_dead", limits={"seats": 100}, status="canceled")
    assert run(r.resolve("acme", NOW))["limits"] == {}


def test_a_past_due_subscription_OUTSIDE_grace_contributes_nothing():
    store, r = _world(grace_days=7)
    _sub(store, "sub_a", limits={"seats": 10}, status="past_due", period_end=NOW - 30 * DAY)
    assert run(r.resolve("acme", NOW))["limits"] == {}


def test_a_past_due_row_with_NO_period_end_is_treated_as_out_of_grace():
    """An unmeasurable overdue is not a short one. Defaulting the other way would grant unlimited
    free usage to any row with a missing field."""
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 10}, status="past_due", period_end=0)
    assert run(r.resolve("acme", NOW))["limits"] == {}


def test_grace_of_zero_means_past_due_never_counts():
    store, r = _world(grace_days=0)
    _sub(store, "sub_a", limits={"seats": 10}, status="past_due", period_end=NOW - 1)
    assert run(r.resolve("acme", NOW))["limits"] == {}


def test_a_status_not_in_the_configured_set_is_ignored():
    store, r = _world(billable_statuses=("active",))
    _sub(store, "sub_t", limits={"seats": 9}, status="trialing")
    assert run(r.resolve("acme", NOW))["limits"] == {}


def test_another_tenants_subscription_never_leaks_in():
    store, r = _world()
    _sub(store, "sub_mine", limits={"seats": 5}, tenant="acme")
    _sub(store, "sub_theirs", limits={"seats": 500}, tenant="globex")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 5


# ── add-ons and replace ───────────────────────────────────────────────────────
def test_an_addon_STACKS_by_default():
    """A wrong stack overcharges nobody; a wrong replace silently removes something paid for."""
    store, r = _world()
    _sub(store, "sub_base", limits={"seats": 10}, created="2024-01-01")
    _sub(store, "sub_addon", limits={"seats": 5}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 15


def test_a_REPLACE_plan_supersedes_everything_before_it():
    store, r = _world()
    _sub(store, "sub_old", limits={"seats": 10, "legacy_flag": True}, created="2024-01-01")
    _sub(store, "sub_new", limits={"seats": 50}, created="2024-01-02", stacking="replace")

    out = run(r.resolve("acme", NOW))
    assert out["limits"]["seats"] == 50
    # Truncated, not merged-then-overwritten: a superseded row cannot leak a key the replacing
    # plan does not mention.
    assert "legacy_flag" not in out["limits"]


def test_a_subscription_AFTER_a_replace_still_stacks_onto_it():
    store, r = _world()
    _sub(store, "sub_old", limits={"seats": 10}, created="2024-01-01")
    _sub(store, "sub_new", limits={"seats": 50}, created="2024-01-02", stacking="replace")
    _sub(store, "sub_extra", limits={"seats": 5}, created="2024-01-03")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 55


# ── 3. no silent answers ──────────────────────────────────────────────────────
def test_every_value_carries_WHO_contributed_what():
    """The answer to the question a customer actually asks: 'I paid for 25, why do I have 20?'"""
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 20}, created="2024-01-01")
    _sub(store, "sub_b", limits={"seats": 5}, created="2024-01-02")

    detail = run(r.resolve("acme", NOW))["detail"]["seats"]
    assert detail["value"] == 25 and detail["strategy"] == "sum"
    assert detail["from"] == [{"subscription": "sub_a", "contributed": 20},
                              {"subscription": "sub_b", "contributed": 5}]


def test_a_key_that_cannot_be_merged_is_REFUSED_WITH_A_REASON_not_dropped():
    """A key missing for a reason nobody can see is the original bug again."""
    store, r = _world(merge_strategies={"tier": "rank"})       # no rank_orders declared
    _sub(store, "sub_a", limits={"tier": "gold", "seats": 5})

    out = run(r.resolve("acme", NOW))
    assert "tier" not in out["limits"]
    assert "RANK needs a declared order" in out["refused"]["tier"]
    assert out["limits"]["seats"] == 5          # one bad key does not poison the rest


def test_mixed_types_across_subscriptions_refuse_rather_than_coerce():
    """Coercion is how '20' and 5 becomes 205."""
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": "twenty"}, created="2024-01-01")
    _sub(store, "sub_b", limits={"seats": 5}, created="2024-01-02")
    out = run(r.resolve("acme", NOW))
    assert "seats" in out["refused"]


def test_the_resolved_subscription_ids_are_reported():
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 1}, created="2024-01-01")
    _sub(store, "sub_b", limits={"seats": 1}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["subscriptions"] == ["sub_a", "sub_b"]


# ── determinism ───────────────────────────────────────────────────────────────
def test_two_rows_sharing_a_created_value_still_resolve_the_SAME_WAY_every_time():
    """REPLACE takes the last contribution, which only means 'newest wins' if the order is total.
    Without the id tie-break the same tenant could resolve two different ways on two reads."""
    store, r = _world()
    _sub(store, "sub_b", limits={"tier": "b"}, created="2024-01-01")
    _sub(store, "sub_a", limits={"tier": "a"}, created="2024-01-01")
    answers = {run(r.resolve("acme", NOW))["limits"]["tier"] for _ in range(5)}
    assert len(answers) == 1


# ── 4. the merge engine, in isolation ─────────────────────────────────────────
def test_a_boolean_never_becomes_a_number():
    """isinstance(True, int) is true in Python, so a type-ordered lookup would give booleans SUM
    and turn two granted flags into 2."""
    out = merge("flag", [{"subscription": "a", "value": True},
                         {"subscription": "b", "value": True}])
    assert out["value"] is True and out["strategy"] == "or"


def test_an_unknown_strategy_REFUSES_rather_than_guessing():
    """A strategy nobody recognises is a config error, and inventing a permissive reading of one
    is how a typo becomes free access to a paid feature."""
    with pytest.raises(MergeRefused):
        merge("k", [{"subscription": "a", "value": 1}], "totally-made-up")


def test_AND_requires_every_subscription_to_grant_it():
    out = merge("k", [{"subscription": "a", "value": True},
                      {"subscription": "b", "value": False}], "and")
    assert out["value"] is False


def test_MIN_ignores_unlimited_rather_than_comparing_a_string():
    out = merge("k", [{"subscription": "a", "value": 5},
                      {"subscription": "b", "value": UNLIMITED}], "min")
    assert out["value"] == 5


def test_a_key_present_on_only_one_subscription_still_resolves():
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 5}, created="2024-01-01")
    _sub(store, "sub_b", limits={"sso": True}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"] == {"seats": 5, "sso": True}


# ── rich limit definitions: the shape prepaid packs actually use ──────────────
# A limit may be a full definition rather than a scalar:
#     {"kind": "counter", "limit": 50000, "period": "billing_period", "overage": "allow_and_bill"}
# Merged as a scalar it falls to REPLACE, so two packs resolve to ONE pack. Counters are exactly
# what a prepaid pack is, so this is the case the whole union exists for.
_PACK = {"kind": "counter", "limit": 50_000, "period": "billing_period",
         "overage": "allow_and_bill"}


def test_TWO_PREPAID_PACKS_ADD_UP():
    """The bug that would have shipped: two 50 000-call packs resolving to 50 000, and the
    customer losing the pack they bought."""
    store, r = _world()
    _sub(store, "sub_p1", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_p2", limits={"api_calls": dict(_PACK)}, created="2024-01-02")

    out = run(r.resolve("acme", NOW))
    assert out["limits"]["api_calls"]["limit"] == 100_000
    assert out["limits"]["api_calls"]["kind"] == "counter"


def test_a_pack_preserves_its_period_and_policy():
    store, r = _world()
    _sub(store, "sub_p1", limits={"api_calls": dict(_PACK)})
    got = run(r.resolve("acme", NOW))["limits"]["api_calls"]
    assert got["period"] == "billing_period" and got["overage"] == "allow_and_bill"


def test_a_COUNTER_and_a_GAUGE_for_one_key_are_refused():
    """A counter is consumed and a gauge is reported. Merging them produces a number that means
    neither."""
    store, r = _world()
    _sub(store, "sub_a", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_b", limits={"api_calls": {"kind": "gauge", "limit": 5}}, created="2024-01-02")

    out = run(r.resolve("acme", NOW))
    assert "api_calls" not in out["limits"]
    assert "disagree on kind" in out["refused"]["api_calls"]


def test_periods_that_disagree_are_refused():
    """50 000 per day and 50 000 per month have no common value."""
    store, r = _world()
    daily = {**_PACK, "period": "day"}
    _sub(store, "sub_a", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_b", limits={"api_calls": daily}, created="2024-01-02")
    assert "disagree on period" in run(r.resolve("acme", NOW))["refused"]["api_calls"]


def test_a_POLICY_CLASH_resolves_to_the_STRICTER_one():
    """Being wrong toward `block` blocks a request and someone complains. Being wrong toward
    `allow_and_bill` silently charges a customer for overage they never agreed to, which costs
    money and trust to undo."""
    store, r = _world()
    _sub(store, "sub_a", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_b", limits={"api_calls": {**_PACK, "overage": "block"}}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["api_calls"]["overage"] == "block"


def test_a_rich_definition_and_a_bare_number_are_the_same_entitlement():
    """One plan states it fully, another states just a cap. Both are that tenant's allowance."""
    store, r = _world()
    _sub(store, "sub_a", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_b", limits={"api_calls": 10}, created="2024-01-02")
    assert run(r.resolve("acme", NOW))["limits"]["api_calls"]["limit"] == 50_010


def test_the_merged_definition_still_evaluates_through_the_normal_check():
    """The union must produce something the EXISTING enforcement understands — a merged limit that
    entitlements.check cannot read would be a cap that never applies."""
    from app.core import entitlements as ent

    store, r = _world()
    _sub(store, "sub_p1", limits={"api_calls": dict(_PACK)}, created="2024-01-01")
    _sub(store, "sub_p2", limits={"api_calls": dict(_PACK)}, created="2024-01-02")
    limits = run(r.resolve("acme", NOW))["limits"]

    assert ent.check(limits, "api_calls", current=99_999)["allowed"] is True
    over = ent.check(limits, "api_calls", current=100_000)
    assert over["allowed"] is True and over["overage"] == 1      # allow_and_bill


def test_a_blocked_counter_refuses_past_the_MERGED_cap():
    from app.core import entitlements as ent

    store, r = _world()
    blocked = {**_PACK, "overage": "block"}
    _sub(store, "sub_a", limits={"scans": dict(blocked)}, created="2024-01-01")
    _sub(store, "sub_b", limits={"scans": dict(blocked)}, created="2024-01-02")
    limits = run(r.resolve("acme", NOW))["limits"]

    assert ent.check(limits, "scans", current=99_999)["allowed"] is True
    assert ent.check(limits, "scans", current=100_000)["allowed"] is False


# ── contract terms: a fixed-term negotiated cap with no plan ──────────────────
def test_a_CONTRACT_TERM_contributes_like_any_other_subscription():
    """An enterprise deal is a subscription with an agreed period and its own snapshot — no plan
    in the catalogue. It needs no new model once resolution unions rather than picks."""
    store, r = _world()
    _sub(store, "sub_plan", limits={"seats": 10}, created="2024-01-01")
    _sub(store, "sub_contract", limits={"seats": 500}, created="2024-01-02", plan_id="")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 510


def test_an_EXPIRED_contract_stops_contributing_once_it_is_cancelled():
    store, r = _world()
    _sub(store, "sub_plan", limits={"seats": 10}, created="2024-01-01")
    _sub(store, "sub_contract", limits={"seats": 500}, created="2024-01-02", status="canceled")
    assert run(r.resolve("acme", NOW))["limits"]["seats"] == 10


# ── fail closed on a lapsed period (a stalled sweep must not read as free service) ──────
def test_an_active_subscription_PAST_its_period_stops_contributing():
    # The defect: status alone granted entitlements, so an active sub whose period ended kept its
    # limits for ever — a paused renewal sweep silently became free service. It must fail CLOSED.
    store, r = _world()                                   # tolerance = 1 day
    _sub(store, "sub_a", limits={"seats": 10}, status="active", period_end=NOW - 2 * DAY)
    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {}                            # dropped, not granted
    assert out["lapsed"] == ["sub_a"]                     # and made visible, not silent


def test_an_active_subscription_WITHIN_the_tolerance_still_counts():
    # A sweep running a little late must not cut anyone off — CYCLE_LEAD_SECONDS renews ahead of the
    # boundary, and the tolerance covers the small gap.
    store, r = _world()                                   # tolerance = 1 day
    _sub(store, "sub_a", limits={"seats": 10}, status="active", period_end=NOW - 3600)
    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {"seats": 10} and out["lapsed"] == []


def test_a_non_renewing_pack_with_NO_period_end_never_lapses():
    # A prepaid pack / open-ended contract has no period to end. `period_end` absent ⇒ it keeps
    # counting, so the fail-closed rule does not break the shapes the union was built for.
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 10}, status="active", period_end=0)
    out = run(r.resolve("acme", NOW))
    assert out["limits"] == {"seats": 10} and out["lapsed"] == []


def test_the_tolerance_is_configurable():
    # Zero tolerance = cut off at the boundary; a longer one keeps a sub alive further past it.
    store, r = _world(period_tolerance_seconds=0)
    _sub(store, "sub_a", limits={"seats": 10}, status="active", period_end=NOW - 1)
    assert run(r.resolve("acme", NOW))["limits"] == {}          # 1s past, no tolerance ⇒ dropped
    store2, r2 = _world(period_tolerance_seconds=int(5 * DAY))
    _sub(store2, "sub_a", limits={"seats": 10}, status="active", period_end=NOW - 2 * DAY)
    assert run(r2.resolve("acme", NOW))["limits"] == {"seats": 10}   # inside 5-day tolerance


def test_a_lapsed_active_subscription_is_logged_loudly(caplog):
    # "A missed sweep should look like an outage." A spike in this WARNING is the alertable signal
    # that renewals have stopped — not an invoice discovered missing at month end.
    import logging
    store, r = _world()
    _sub(store, "sub_a", limits={"seats": 10}, status="active", period_end=NOW - 2 * DAY)
    with caplog.at_level(logging.WARNING, logger="app.entitlements"):
        run(r.resolve("acme", NOW))
    assert any(rec.msg == "entitlement_period_lapsed" for rec in caplog.records)


def test_a_past_due_lapse_is_ordinary_dunning_not_the_alarm(caplog):
    # A past_due sub falling out of grace is the expected end of dunning, not a stalled sweep, so it
    # is INFO, not the WARNING that signals renewals have stopped. It is still reported as lapsed.
    import logging
    store, r = _world(grace_days=7)
    _sub(store, "sub_a", limits={"seats": 10}, status="past_due", period_end=NOW - 30 * DAY)
    with caplog.at_level(logging.INFO, logger="app.entitlements"):
        out = run(r.resolve("acme", NOW))
    assert out["lapsed"] == ["sub_a"]
    msgs = {(rec.levelno, rec.msg) for rec in caplog.records}
    assert (logging.INFO, "entitlement_dunning_lapsed") in msgs
    assert not any(rec.msg == "entitlement_period_lapsed" for rec in caplog.records)
