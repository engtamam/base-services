"""subscribe_with_payment — the charge-first front door for POST /subscriptions.

The vulnerability it closes: the bare subscribe() primitive marks a PAID plan `active` and stores the
token but charges NOTHING until the next renewal — a self-service caller could hold an entitled paid
plan free for a full cycle. This wrapper never activates a paid plan without invoicing + charging it
now (or refusing when there is no way to charge). Trial/free plans are unaffected.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox
from app.core.models import iso, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PayLinkStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


class _Charge:
    def __init__(self, k, ok=True):
        self.ok = ok
        self.status = "succeeded" if ok else "failed"
        self.provider = "mock"
        self.provider_id = "ch_" + str(k.get("idempotency_key", "x"))[:12]
        self.amount_minor = k.get("amount_minor", 0)
        self.currency = k.get("currency", "SAR")
        self.error = "" if ok else "CARD_DECLINED"
        self.raw = {}


class MockGW:
    driver = "mock"

    def __init__(self, ok=True):
        self._ok = ok

    def charge(self, **k):
        return _Charge(k, ok=self._ok)

    def verify_webhook(self, **k):
        return None


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, gw=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active", "trialing"),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active",
           "pay_link_ttl_seconds": 3600}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        paylinks=PayLinkStore(st), gateway=gw or MockGW(), tax=MockTax(), einvoicer=None,
        pdf=None, grants=_outbox(st), config=cfg)


def _plan(b, amount=10000, interval="month", trial_days=0):
    return run(b.create_plan(name="App", trial_days=trial_days,
                             prices=[{"interval": interval, "amount_minor": amount,
                                      "currency": "SAR", "type": "flat"}], limits={"credits": 100}))


def _invoices_for(st, sub_id):
    rows, _ = run(InvoiceStore(st)._store.find("invoices", {"subscription_id": sub_id}, limit=10))
    return rows


# ── the fix ───────────────────────────────────────────────────────────────────────

def test_paid_plan_with_token_is_charged_now_not_free():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    res = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_1",
                                       idempotency_key="k1"))
    assert res["status"] == "active" and res["paid"] is True
    assert res["invoice_id"]                                   # an invoice was actually issued...
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert inv.status == "paid" and inv.total_minor == 11500   # ...and charged (10000 + 15% VAT)
    pay, _ = run(PaymentStore(st)._store.find("payments", {"invoice_id": inv.id}, limit=5))
    assert pay and pay[0]["status"] == "succeeded"


def test_paid_plan_without_a_token_is_refused_and_creates_nothing():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    with pytest.raises(ValueError, match="PAYMENT_REQUIRED"):
        run(b.subscribe_with_payment(tenant="t1", plan_id=p.id))       # no token → no free access
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert subs == []                                          # nothing dangling


def test_a_declined_card_still_attempted_the_charge_and_left_an_invoice():
    """A decline is not the vuln: the invoice is CUT and the charge ATTEMPTED (then dunned) — unlike
    the old path that never billed at all."""
    st = MemoryDataStore(); b = _svc(st, gw=MockGW(ok=False))
    p = _plan(b, amount=10000)
    res = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_bad",
                                       idempotency_key="k1"))
    assert res["paid"] is False and res["invoice_id"]
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert inv.status != "paid"                                # unpaid, in dunning — but it EXISTS
    pay, _ = run(PaymentStore(st)._store.find("payments", {"invoice_id": inv.id}, limit=5))
    assert pay and pay[0]["status"] in ("failed", "pending")  # a real charge was attempted


# ── trial / free are unaffected (no payment is due) ──────────────────────────────────

def test_a_trial_plan_needs_no_charge():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000, trial_days=14)
    res = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    assert res["status"] == "trialing" and res["invoice_id"] == ""
    assert _invoices_for(st, res["id"]) == []                 # no invoice cut for a trial


def test_a_free_plan_activates_without_an_invoice():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=0)
    res = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id))
    assert res["status"] == "active" and res["invoice_id"] == ""
    assert _invoices_for(st, res["id"]) == []


# ── idempotency & robustness ────────────────────────────────────────────────────────

def test_retried_paid_signup_charges_only_once():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    a = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_1",
                                     idempotency_key="k1"))
    c = run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_1",
                                     idempotency_key="k1"))
    assert c["invoice_id"] == a["invoice_id"]                 # replay, same invoice
    invs, _ = run(InvoiceStore(st)._store.find("invoices", {"tenant": "t1"}, limit=10))
    assert len(invs) == 1                                     # charged exactly once
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert len(subs) == 1


def test_unknown_or_inactive_plan_is_refused():
    st = MemoryDataStore(); b = _svc(st)
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        run(b.subscribe_with_payment(tenant="t1", plan_id="nope", payment_token="pm_1"))
    p = run(b.create_plan(name="Retired", prices=[{"interval": "month", "amount_minor": 10000,
            "currency": "SAR", "type": "flat"}], limits={}))
    row = run(PlanStore(st).get(p.id)); row["active"] = False    # retire it
    run(PlanStore(st).put(p.id, row))
    with pytest.raises(ValueError, match="PLAN_NOT_AVAILABLE"):
        run(b.subscribe_with_payment(tenant="t1", plan_id=p.id, payment_token="pm_1"))
