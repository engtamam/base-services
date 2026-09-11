"""Percent-of-price included allowance: an opted-in plan grants PLAN_CREDIT_PERCENT% of its price
each cycle, from a runtime-editable global knob, instead of a static credits limit.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox
from app.core.stores import (CouponStore, DiscountRuleStore, IdempotencyStore, InvoiceStore,
                             LedgerStore, PaymentStore, PlanStore, SettlementStore,
                             SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class MockGW:
    driver = "mock"

    def charge(self, **k):
        return _Charge(k)

    def verify_webhook(self, **k):
        return None


class _Charge:
    def __init__(self, k):
        self.ok, self.status, self.provider = True, "succeeded", "mock"
        self.provider_id = "ch_x"
        self.amount_minor = k.get("amount_minor", 0)
        self.currency, self.error, self.raw = k.get("currency", "SAR"), "", {}


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        return 0                                   # tax irrelevant to the allowance test


def _svc(st, percent=0):
    from app.core.models import iso, now_ts
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "ai_credit", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "reset", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active",
           "plan_credit_percent": percent}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), discount_rules=DiscountRuleStore(st),
        subs=SubscriptionStore(st), invoices=InvoiceStore(st), payments=PaymentStore(st),
        usage=UsageStore(st), ledger=LedgerStore(st), idempo=IdempotencyStore(st),
        settlements=SettlementStore(st), gateway=MockGW(), tax=MockTax(), einvoicer=None, pdf=None,
        grants=GrantOutbox(st, None, clock=now_ts, iso=iso,
                           cfg={"grant_fund": "ai_credit", "grant_retry_base_seconds": 30,
                                "grant_retry_max_seconds": 3600}),
        config=cfg)


def _grant_of(inv):
    return int((inv.meta or {}).get("grant_minor", 0) or 0)


def test_opted_in_plan_grants_percent_of_price():
    st = MemoryDataStore(); b = _svc(st, percent=20)
    p = run(b.create_plan(name="AI Bot", prices=[{"interval": "month", "amount_minor": 19900,
            "currency": "SAR", "type": "flat"}], credit_percent_of_price=True))
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    inv = run(b.run_cycle(s.id))
    assert _grant_of(inv) == 3980                      # 20% of 19900


def test_percent_change_moves_the_next_cycle_only():
    st = MemoryDataStore(); b = _svc(st, percent=20)
    p = run(b.create_plan(name="AI Bot", prices=[{"interval": "month", "amount_minor": 19900,
            "currency": "SAR", "type": "flat"}], credit_percent_of_price=True))
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    assert _grant_of(run(b.run_cycle(s.id))) == 3980
    # operator raises the included allowance to 30% — a NEW service instance reads the new global value
    b2 = _svc(st, percent=30)
    inv2 = run(b2.run_cycle(s.id))
    assert _grant_of(inv2) == 5970                     # 30% of 19900, next cycle


def test_non_opted_plan_keeps_static_credits():
    st = MemoryDataStore(); b = _svc(st, percent=20)
    p = run(b.create_plan(name="Fixed", prices=[{"interval": "month", "amount_minor": 10000,
            "currency": "SAR", "type": "flat"}], limits={"credits": 500}))
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    inv = run(b.run_cycle(s.id))
    assert _grant_of(inv) == 500                       # static, untouched by the percent knob


def test_list_plans_adds_included_credit_minor_per_price_when_opted_in():
    st = MemoryDataStore(); b = _svc(st, percent=20)
    run(b.create_plan(name="AI Bot", prices=[
        {"interval": "month", "amount_minor": 19900, "currency": "SAR", "type": "flat"},
        {"interval": "year", "amount_minor": 199000, "currency": "SAR", "type": "flat"}],
        credit_percent_of_price=True))
    run(b.create_plan(name="Fixed", prices=[{"interval": "month", "amount_minor": 10000,
        "currency": "SAR", "type": "flat"}], limits={"credits": 500}))
    plans = {p["name"]: p for p in run(b.list_plans())}
    ai_prices = {pr["interval"]: pr for pr in plans["AI Bot"]["prices"]}
    assert ai_prices["month"]["included_credit_minor"] == 3980      # 20% of 19900, per price
    assert ai_prices["year"]["included_credit_minor"] == 39800      # 20% of 199000
    # a non-opted plan's prices carry no such field
    assert "included_credit_minor" not in plans["Fixed"]["prices"][0]


def test_included_credit_minor_does_not_mutate_the_stored_plan():
    st = MemoryDataStore(); b = _svc(st, percent=20)
    p = run(b.create_plan(name="AI Bot", prices=[{"interval": "month", "amount_minor": 19900,
        "currency": "SAR", "type": "flat"}], credit_percent_of_price=True))
    run(b.list_plans())
    stored = run(PlanStore(st).fetch(p.id))
    assert "included_credit_minor" not in stored.prices[0]          # the view field never persisted


def test_opted_in_but_percent_zero_grants_nothing():
    st = MemoryDataStore(); b = _svc(st, percent=0)
    p = run(b.create_plan(name="AI Bot", prices=[{"interval": "month", "amount_minor": 19900,
            "currency": "SAR", "type": "flat"}], credit_percent_of_price=True))
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    inv = run(b.run_cycle(s.id))
    assert _grant_of(inv) == 0                         # feature off until the operator sets a percent
