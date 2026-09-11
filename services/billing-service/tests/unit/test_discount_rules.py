"""Cross-app discount rules: pure composition (MAX) + live per-cycle application at renewal.

Money safety: a rule discounts a renewal only while the tenant HOLDS the owning key (read through the
EntitlementResolver); several qualifying rules take the HIGHEST percent, never summed; the larger of
coupon vs rule wins (never summed); a rule can reach 100% (free) but never negative; and a rule added
today moves the NEXT cycle only — grandfathering is intact because pricing is computed per cycle.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core import pricing
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox
from app.core.stores import (CouponStore, DiscountRuleStore, IdempotencyStore, InvoiceStore,
                             LedgerStore, PaymentStore, PlanStore, SettlementStore,
                             SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


# ── pure composition helpers ─────────────────────────────────────────────────
def _rule(owning, target, pct, **k):
    return {"id": f"dr_{owning}_{target}_{pct}", "owning_key": owning, "target_key": target,
            "percent": pct, "active": True, **k}


def test_best_rule_percent_takes_the_max_never_sums():
    rules = [_rule("feature.visit", "feature.bookings", 10),
             _rule("feature.loyalty", "feature.bookings", 25),
             _rule("feature.visit", "feature.assistant", 90)]   # different target — must not count
    pct, rid = pricing.best_rule_percent(
        rules, held_keys={"feature.visit", "feature.loyalty"}, target_keys={"feature.bookings"})
    assert pct == 25 and rid.endswith("_25")                    # highest applicable, not 10+25=35


def test_rule_ignored_when_owner_not_held_or_target_mismatch():
    rules = [_rule("feature.visit", "feature.bookings", 50)]
    assert pricing.best_rule_percent(rules, held_keys=set(), target_keys={"feature.bookings"})[0] == 0
    assert pricing.best_rule_percent(rules, held_keys={"feature.visit"}, target_keys={"other"})[0] == 0


def test_rule_window_gates_the_discount():
    rules = [_rule("x", "y", 50, starts_at=100.0, expires_at=200.0)]
    kw = {"held_keys": {"x"}, "target_keys": {"y"}}
    assert pricing.best_rule_percent(rules, now=50.0, **kw)[0] == 0     # before start
    assert pricing.best_rule_percent(rules, now=150.0, **kw)[0] == 50   # inside
    assert pricing.best_rule_percent(rules, now=250.0, **kw)[0] == 0    # after end


def test_rule_discount_caps_at_subtotal_and_reaches_free():
    disc, _ = pricing.rule_discount([_rule("x", "y", 100)], 10000, held_keys={"x"}, target_keys={"y"})
    assert disc == 10000                                        # 100% = fully free, never more


# ── live application at renewal ──────────────────────────────────────────────
class MockGW:
    driver = "mock"

    def charge(self, **k):
        return _Charge(k)

    def verify_webhook(self, **k):
        return None


class _Charge:
    def __init__(self, k):
        self.ok, self.status, self.provider = True, "succeeded", "mock"
        self.provider_id = "ch_" + k.get("idempotency_key", "x")[:8]
        self.amount_minor = k.get("amount_minor", 0)
        self.currency, self.error, self.raw = k.get("currency", "SAR"), "", {}


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


def _svc(st):
    from app.core.models import iso, now_ts
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active"}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), discount_rules=DiscountRuleStore(st),
        subs=SubscriptionStore(st), invoices=InvoiceStore(st), payments=PaymentStore(st),
        usage=UsageStore(st), ledger=LedgerStore(st), idempo=IdempotencyStore(st),
        settlements=SettlementStore(st), gateway=MockGW(), tax=MockTax(), einvoicer=None, pdf=None,
        grants=GrantOutbox(st, None, clock=now_ts, iso=iso,
                           cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                                "grant_retry_max_seconds": 3600}),
        config=cfg)


def _two_apps(b, st, tenant="t1", bookings_coupon=""):
    # app X ("visit") grants the limit key app.visit; app Y ("bookings") grants app.bookings.
    px = run(b.create_plan(name="Visit", prices=[{"interval": "month", "amount_minor": 3000,
             "currency": "SAR", "type": "flat"}], limits={"app.visit": 1, "credits": 10}))
    py = run(b.create_plan(name="Bookings", prices=[{"interval": "month", "amount_minor": 10000,
             "currency": "SAR", "type": "flat"}], limits={"app.bookings": 1, "credits": 50}))
    run(b.subscribe(tenant=tenant, plan_id=px.id, payment_token="pm_1"))
    sy = run(b.subscribe(tenant=tenant, plan_id=py.id, payment_token="pm_1",
                         coupon_code=bookings_coupon))
    return sy


def _renew(b, st, sub_id):
    inv = run(b.run_cycle(sub_id))
    return inv


def test_rule_discounts_the_renewal_only_while_owned():
    st = MemoryDataStore(); b = _svc(st)
    sy = _two_apps(b, st)
    run(b.create_discount_rule(owning_key="app.visit", target_key="app.bookings", percent=50))
    inv = _renew(b, st, sy.id)
    # 10000 subtotal - 50% = 5000 taxable, +15% VAT = 5750
    assert inv.discount_minor == 5000 and inv.total_minor == 5750


def test_no_rule_prices_full():
    st = MemoryDataStore(); b = _svc(st)
    sy = _two_apps(b, st)
    inv = _renew(b, st, sy.id)
    assert inv.discount_minor == 0 and inv.total_minor == 11500       # 10000 + 15%


def test_rule_not_owned_does_not_apply():
    st = MemoryDataStore(); b = _svc(st)
    sy = _two_apps(b, st)
    # owning a key the tenant does NOT hold → no discount
    run(b.create_discount_rule(owning_key="app.nonexistent", target_key="app.bookings", percent=50))
    inv = _renew(b, st, sy.id)
    assert inv.discount_minor == 0


def test_max_wins_between_coupon_and_rule():
    st = MemoryDataStore(); b = _svc(st)
    run(b.upsert_coupon(code="TEN", type="percent", value=10, active=True))
    sy = _two_apps(b, st, bookings_coupon="TEN")
    run(b.create_discount_rule(owning_key="app.visit", target_key="app.bookings", percent=40))
    inv = _renew(b, st, sy.id)
    # max(10% coupon, 40% rule) = 40% → discount 4000 (never 50% summed)
    assert inv.discount_minor == 4000


def test_full_free_rule():
    st = MemoryDataStore(); b = _svc(st)
    sy = _two_apps(b, st)
    run(b.create_discount_rule(owning_key="app.visit", target_key="app.bookings", percent=100))
    inv = _renew(b, st, sy.id)
    assert inv.discount_minor == 10000 and inv.total_minor == 0        # fully free


def test_discount_preview_shows_qualifying_plans_only():
    st = MemoryDataStore(); b = _svc(st)
    # tenant owns "visit" (holds app.visit). The bookings plan is listed & buyable.
    _two_apps(b, st)
    run(b.create_discount_rule(owning_key="app.visit", target_key="app.bookings", percent=30))
    prev = run(b.discount_preview("t1"))
    # the bookings plan is previewed at 30%, naming the app that earned it
    bookings = next(v for k, v in prev.items() if v["percent"] == 30)
    assert bookings["owning_key"] == "app.visit"


def test_discount_preview_empty_when_owner_not_held():
    st = MemoryDataStore(); b = _svc(st)
    _two_apps(b, st)
    run(b.create_discount_rule(owning_key="app.nonexistent", target_key="app.bookings", percent=30))
    assert run(b.discount_preview("t1")) == {}


def test_crud_percent_out_of_range_refused():
    import pytest
    st = MemoryDataStore(); b = _svc(st)
    with pytest.raises(ValueError):
        run(b.create_discount_rule(owning_key="a", target_key="b", percent=150))
