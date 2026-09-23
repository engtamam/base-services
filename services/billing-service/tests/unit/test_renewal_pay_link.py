"""Renewal collection on a TOKEN-LESS gateway (hosted-checkout only, e.g. TuwaiqPay).

There is no saved card to charge, so a renewal must not dun a card forever. Instead the cycle cuts the
invoice, mints a hosted pay link for it, and emits SUBSCRIPTION_RENEWAL_PAYMENT_LINK for a channel to
deliver. The invoice is settled through the normal webhook/reconcile seam when the payer pays.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core import events as ev
from app.core.billing_service import BillingService
from app.core.events import Emitter
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
    def __init__(self, status):
        self.status = status


class TokenlessGW:
    """A hosted-checkout-only gateway: prepare_checkout + fetch_status, and NO token charge."""
    driver = "tuwaiqpay-like"
    supports_token_charge = False

    def __init__(self):
        self.settled: set[str] = set()
        self.charge_calls = 0

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        return CheckoutSession(ok=True, provider="hosted", id=f"sess_{reference}",
                               redirect_url=f"https://checkout.hosted/pay/{reference}")

    def fetch_status(self, *, provider_id, currency):
        return _Status("succeeded" if provider_id in self.settled else "pending")

    def charge(self, **k):
        self.charge_calls += 1                    # must NEVER be called for a token-less renewal
        raise AssertionError("token charge attempted on a token-less gateway")


class _Bus:
    def __init__(self):
        self.events: list = []

    async def publish(self, topic, event):
        self.events.append(event)

    def one(self, kind):
        found = [e for e in self.events if e.type == kind]
        assert found, f"{kind} not published — got {[e.type for e in self.events]}"
        return found[0]

    def none(self, kind):
        assert not [e for e in self.events if e.type == kind], f"{kind} was published unexpectedly"


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, gw, bus):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active", "trialing"),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active",
           "pay_link_ttl_seconds": 3600, "pay_link_base_url": "https://pay.example.com/l"}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        paylinks=PayLinkStore(st), gateway=gw, tax=MockTax(), einvoicer=None, pdf=None,
        grants=_outbox(st), emitter=Emitter(bus), config=cfg)


def _due_sub(b, st, amount=10000):
    p = run(b.create_plan(name="App", prices=[{"interval": "month", "amount_minor": amount,
            "currency": "SAR", "type": "flat"}], limits={"credits": 100}))
    s = run(b.subscribe(tenant="t1", plan_id=p.id))          # token-less: no payment_token
    row = run(SubscriptionStore(st).get(s.id))
    row["current_period_end"] = now_ts() - 1                 # force it due
    run(SubscriptionStore(st).put(s.id, row))
    return run(SubscriptionStore(st).fetch(s.id))


def test_token_less_renewal_mints_a_pay_link_and_emits_it_without_charging():
    st, bus = MemoryDataStore(), _Bus()
    gw = TokenlessGW(); b = _svc(st, gw, bus)
    sub = _due_sub(b, st)
    inv = run(b.run_cycle(sub.id))
    assert gw.charge_calls == 0                              # no doomed card charge
    inv = run(InvoiceStore(st).fetch(inv.id))
    assert inv.status == "open"                              # invoice cut, unpaid, awaiting the link
    e = bus.one(ev.SUBSCRIPTION_RENEWAL_PAYMENT_LINK)
    assert e.payload["invoice_id"] == inv.id
    assert e.payload["subscription_id"] == sub.id
    assert e.payload["amount_minor"] == 11500               # 10000 + 15% VAT
    assert e.payload["checkout_url"].startswith("https://checkout.hosted/pay/")
    assert e.payload["pay_url"].startswith("https://pay.example.com/l/")   # stable redirector link
    assert e.payload["token"]
    bus.none(ev.PAYMENT_SUCCEEDED)                           # nothing paid yet
    # a pending payment (bound to the gateway session) exists for the reconcile poller to settle
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv.id}"))
    assert pay and pay.status == "pending"


def test_the_minted_link_settles_the_invoice_when_paid():
    st, bus = MemoryDataStore(), _Bus()
    gw = TokenlessGW(); b = _svc(st, gw, bus)
    sub = _due_sub(b, st)
    inv = run(b.run_cycle(sub.id))
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv.id}"))
    gw.settled.add(pay.provider_id)                          # the payer completes the hosted checkout
    out = run(b.reconcile_payment(pay.id))
    assert out["reconciled"] is True and out["payment_status"] == "succeeded"
    assert run(InvoiceStore(st).fetch(inv.id)).status == "paid"
    bus.one(ev.INVOICE_PAID)


def test_a_token_gateway_still_charges_normally_no_link():
    """Regression guard: a gateway that DOES support token charge is unaffected — it charges as before
    and emits no renewal-payment-link."""
    st, bus = MemoryDataStore(), _Bus()

    class TokenGW:
        driver = "card"                                     # supports_token_charge defaults True

        def charge(self, **k):
            from app.ports.payment_gateway import Charge
            return Charge(ok=True, provider_id="ch_1", status="succeeded")

        def verify_webhook(self, **k):
            raise NotImplementedError

    b = _svc(st, TokenGW(), bus)
    p = run(b.create_plan(name="App", prices=[{"interval": "month", "amount_minor": 10000,
            "currency": "SAR", "type": "flat"}], limits={"credits": 100}))
    s = run(b.subscribe(tenant="t1", plan_id=p.id, payment_token="pm_1"))
    row = run(SubscriptionStore(st).get(s.id)); row["current_period_end"] = now_ts() - 1
    run(SubscriptionStore(st).put(s.id, row))
    inv = run(b.run_cycle(s.id))
    assert run(InvoiceStore(st).fetch(inv.id)).status == "paid"
    bus.none(ev.SUBSCRIPTION_RENEWAL_PAYMENT_LINK)
    bus.one(ev.PAYMENT_SUCCEEDED)


def test_renewal_link_is_idempotent_across_a_re_run():
    """Re-running the same cycle must not mint a SECOND checkout session (single-use per invoice)."""
    st, bus = MemoryDataStore(), _Bus()
    gw = TokenlessGW(); b = _svc(st, gw, bus)
    sub = _due_sub(b, st)
    inv = run(b.run_cycle(sub.id))
    # a duplicate cycle run replays the same invoice; the pay link is single-use per invoice
    run(b.run_cycle(sub.id))
    pend, _ = run(PaymentStore(st)._store.find("payments", {"invoice_id": inv.id}, limit=10))
    assert len([p for p in pend if p["status"] == "pending"]) == 1     # exactly ONE checkout session
