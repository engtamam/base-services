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
        "grandfather": True, "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}}


def run(c):
    return asyncio.run(c)


def _world(**over):
    store = MemoryDataStore()
    cfg = {**_CFG, **over}
    return store, EntitlementResolver(subs=SubscriptionStore(store), plans=PlanStore(store),
                                      cfg=cfg)


def _sub(store, sub_id, *, limits=None, status="active", created="2024-01-01",
         period_end=NOW + DAY, plan_id="", stacking=None, tenant="acme"):
    row = {"id": sub_id, "tenant": tenant, "status": status, "created": created,
           "plan_id": plan_id, "limits_snapshot": limits or {},
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
