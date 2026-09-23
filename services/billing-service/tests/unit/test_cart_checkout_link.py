"""start_cart_checkout — a MULTI-PLAN cart that returns ONE hosted-checkout link (token-less gateways).

Same cart validation and one-invoice semantics as checkout(), but every subscription is created
`incomplete` and they all activate together only when the single cart invoice is paid.
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
from app.ports.payment_gateway import CheckoutSession


def run(c):
    return asyncio.run(c)


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


class _Status:
    def __init__(self, s):
        self.status = s


class HostedGW:
    driver = "hosted"
    supports_token_charge = False

    def __init__(self):
        self.settled: set[str] = set()

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        return CheckoutSession(ok=True, provider="hosted", id=f"sess_{reference}",
                               redirect_url=f"https://checkout.hosted/pay/{reference}")

    def fetch_status(self, *, provider_id, currency):
        return _Status("succeeded" if provider_id in self.settled else "pending")


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st):
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
        paylinks=PayLinkStore(st), gateway=HostedGW(), tax=MockTax(), einvoicer=None, pdf=None,
        grants=_outbox(st), config=cfg)


def _plan(b, name, amount, trial_days=0):
    return run(b.create_plan(name=name, trial_days=trial_days,
                             prices=[{"interval": "month", "amount_minor": amount,
                                      "currency": "SAR", "type": "flat"}], limits={"credits": 10}))


def test_cart_creates_incomplete_subs_and_one_link():
    st = MemoryDataStore(); b = _svc(st)
    p1 = _plan(b, "A", 10000); p2 = _plan(b, "B", 5000)
    res = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p2.id}],
                                    idempotency_key="k1"))
    assert res["checkout_required"] is True
    assert res["amount_minor"] == int(15000 * 1.15)          # 17250: both plans + 15% VAT, ONE invoice
    assert res["checkout_url"] and res["token"]
    assert len(res["items"]) == 2
    for it in res["items"]:
        sub = run(SubscriptionStore(st).fetch(it["subscription_id"]))
        assert sub.status == "incomplete"
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert inv.status == "open" and len(inv.lines) == 2 and (inv.meta or {}).get("checkout_start")
    assert len((inv.meta or {}).get("checkout_subs") or []) == 2


def test_paying_the_cart_invoice_activates_every_sub():
    st = MemoryDataStore(); b = _svc(st)
    p1 = _plan(b, "A", 10000); p2 = _plan(b, "B", 5000)
    res = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p2.id}],
                                    idempotency_key="k1"))
    ends = {it["subscription_id"]: int(run(SubscriptionStore(st).fetch(it["subscription_id"])).current_period_end)
            for it in res["items"]}
    pay = run(PaymentStore(st).by_provider_id(f"sess_{res['invoice_id']}"))
    b._gw.settled.add(pay.provider_id)
    out = run(b.reconcile_payment(pay.id))
    assert out["reconciled"] is True
    assert run(InvoiceStore(st).fetch(res["invoice_id"])).status == "paid"
    for sid, billed_end in ends.items():
        sub = run(SubscriptionStore(st).fetch(sid))
        assert sub.status == "active"                        # ALL subs activated together
        assert int(sub.current_period_start) == billed_end  # each period claimed + advanced
        assert int(sub.current_period_end) == billed_end + 30 * 86400
        assert run(b._idempo.seen(f"cycle:{sid}:{billed_end}")) is not None


def test_mixed_trial_and_paid_only_bills_the_paid_items():
    st = MemoryDataStore(); b = _svc(st)
    paid = _plan(b, "Paid", 10000); trial = _plan(b, "Trial", 8000, trial_days=14)
    res = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": paid.id}, {"plan_id": trial.id}],
                                    idempotency_key="k1"))
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert len(inv.lines) == 1 and inv.total_minor == int(10000 * 1.15)   # only the paid item billed
    statuses = {it["plan_id"]: it["status"] for it in res["items"]}
    assert statuses[paid.id] == "incomplete" and statuses[trial.id] == "trialing"
    # paying activates the paid one; the trial stays trialing
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv.id}"))
    b._gw.settled.add(pay.provider_id)
    run(b.reconcile_payment(pay.id))
    for it in res["items"]:
        sub = run(SubscriptionStore(st).fetch(it["subscription_id"]))
        assert sub.status == ("active" if it["plan_id"] == paid.id else "trialing")


def test_all_trial_cart_needs_no_link():
    st = MemoryDataStore(); b = _svc(st)
    t1 = _plan(b, "T1", 10000, trial_days=7); t2 = _plan(b, "T2", 5000, trial_days=7)
    res = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": t1.id}, {"plan_id": t2.id}],
                                    idempotency_key="k1"))
    assert res["checkout_required"] is False and res["invoice_id"] == ""
    for it in res["items"]:
        assert run(SubscriptionStore(st).fetch(it["subscription_id"])).status == "trialing"


def test_retry_replays_the_same_cart_link_no_second_set():
    st = MemoryDataStore(); b = _svc(st)
    p1 = _plan(b, "A", 10000); p2 = _plan(b, "B", 5000)
    a = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p2.id}],
                                  idempotency_key="k1"))
    c = run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p2.id}],
                                  idempotency_key="k1"))
    assert c == a
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=20))
    assert len(subs) == 2                                    # not four
    invs, _ = run(InvoiceStore(st)._store.find("invoices", {"tenant": "t1"}, limit=20))
    assert len(invs) == 1


def test_a_bad_cart_creates_nothing():
    st = MemoryDataStore(); b = _svc(st)
    p1 = _plan(b, "A", 10000)
    with pytest.raises(ValueError):                          # duplicate plan in the cart
        run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p1.id}],
                                  idempotency_key="k1"))
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=20))
    assert subs == []
    invs, _ = run(InvoiceStore(st)._store.find("invoices", {"tenant": "t1"}, limit=20))
    assert invs == []


def test_mixed_currency_cart_is_refused():
    st = MemoryDataStore(); b = _svc(st)
    p1 = _plan(b, "A", 10000)
    p2 = run(b.create_plan(name="EUR", prices=[{"interval": "month", "amount_minor": 5000,
             "currency": "EUR", "type": "flat"}], limits={}))
    with pytest.raises(ValueError):
        run(b.start_cart_checkout(tenant="t1", items=[{"plan_id": p1.id}, {"plan_id": p2.id}],
                                  idempotency_key="k1"))
    assert run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=20))[0] == []
