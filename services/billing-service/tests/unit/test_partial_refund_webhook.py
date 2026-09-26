"""Audit FIN-015 — a gateway refund WEBHOOK is now amount-based (partial vs full), for ALL gateways.

A partial refund reverses ONLY the refunded amount and leaves the invoice PAID; the invoice voids only
when the cumulative refunded reaches the total. A gateway that reports no amount falls back to a full
refund. Semantics are cumulative-floor, so cumulative-reporting gateways (Moyasar/Stripe) and
per-event gateways (Tap/Checkout/PayPal/…) are both correct, and a replay never double-counts.
"""
from __future__ import annotations

import asyncio
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core import events as ev
from app.core.billing_service import BillingService
from app.core.events import Emitter
from app.core.grants import GrantOutbox
from app.core.models import Invoice, Payment, iso, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import WebhookEvent

SECRET = "whsec"
TENANT = "t1"


def run(c):
    return asyncio.run(c)


class _Bus:
    def __init__(self): self.events = []
    async def publish(self, topic, event): self.events.append(event)
    def refunds(self): return [e for e in self.events if e.type == ev.PAYMENT_REFUNDED]


class RefundGW:
    """Body encodes {type, pid, eid, refunded} so a test controls the refunded amount + event id."""
    driver = "rf"

    def verify_webhook(self, *, headers, body, secret):
        e = json.loads(body)
        return WebhookEvent(verified=(secret == SECRET), type=e.get("type", ""),
                            provider_id=e.get("pid", ""), event_id=e.get("eid", ""),
                            refunded_minor=int(e.get("refunded", 0)))


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, bus, *, on_partial=True):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "grant_fund": "credits",
           "plan_credit_grant_mode": "accumulate", "clawback_enabled": True,
           "clawback_allow_negative": True, "clawback_on_partial": on_partial,
           "clawback_skip_expired": True}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=RefundGW(), tax=None, einvoicer=None, pdf=None, grants=_outbox(st),
        emitter=Emitter(bus), config=cfg)


def _seed_paid(st, total=10000, grant=1000):
    inv = Invoice(id="inv_1", tenant=TENANT, number="INV-1", status="paid", total_minor=total,
                  subtotal_minor=total, currency="SAR", created=iso(),
                  meta={"grant_minor": grant} if grant else {})
    run(InvoiceStore(st).save(inv))
    pay = Payment(id="pay_1", tenant=TENANT, invoice_id="inv_1", amount_minor=total, currency="SAR",
                  status="succeeded", provider_id="ch1", created=iso())
    run(PaymentStore(st).save(pay))
    return inv


def _refund(b, refunded, eid="r1"):
    body = json.dumps({"type": "refund", "pid": "ch1", "eid": eid, "refunded": refunded}).encode()
    return run(b.handle_webhook("rf", {}, body, SECRET, webhook_tenant=TENANT))


def _inv(st):
    return run(InvoiceStore(st).fetch("inv_1"))


# ── partial vs full ───────────────────────────────────────────────────────────────
def test_partial_refund_keeps_the_invoice_paid():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=3000)
    inv = _inv(st)
    assert inv.status == "paid" and inv.refunded_minor == 3000     # NOT voided for a partial (FIN-015)
    assert b_bus_amount(bus) == 3000


def b_bus_amount(bus):
    r = bus.refunds()
    return r[-1].payload["amount_minor"] if r else None


def test_full_refund_voids_the_invoice():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=10000)
    inv = _inv(st)
    assert inv.status == "void" and inv.refunded_minor == 10000
    assert run(PaymentStore(st).fetch("pay_1")).status == "refunded"


def test_partial_then_remainder_voids():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=4000, eid="r1")                            # per-event partial
    assert _inv(st).status == "paid" and _inv(st).refunded_minor == 4000
    _refund(b, refunded=10000, eid="r2")                           # cumulative reaches total
    assert _inv(st).status == "void" and _inv(st).refunded_minor == 10000


def test_no_amount_reported_falls_back_to_full_refund():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=0)                                         # a bare "refunded" signal
    assert _inv(st).status == "void" and _inv(st).refunded_minor == 10000


def test_overreported_amount_is_capped_at_total():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=99999)                                     # gateway over-reports
    inv = _inv(st)
    assert inv.status == "void" and inv.refunded_minor == 10000    # never past the total


def test_replay_of_the_same_refund_event_does_not_double():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=3000, eid="r1")
    res2 = _refund(b, refunded=3000, eid="r1")                     # same event id → deduped upstream
    assert res2.get("duplicate") is True
    assert _inv(st).refunded_minor == 3000                        # not 6000


def test_two_distinct_small_partials_accumulate_when_reported_cumulatively():
    st = MemoryDataStore(); bus = _Bus(); b = _svc(st, bus)
    _seed_paid(st, total=10000)
    _refund(b, refunded=3000, eid="r1")                           # cumulative 3000
    _refund(b, refunded=5000, eid="r2")                           # cumulative 5000
    assert _inv(st).status == "paid" and _inv(st).refunded_minor == 5000
