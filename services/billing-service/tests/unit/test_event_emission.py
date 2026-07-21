"""Events fired by the real service on real money movements.

`test_events.py` exercises the emitter against a fake bus, which proves the envelope and nothing
about whether billing ever calls it. These drive an actual subscribe → charge → refund → cancel and
assert on what came out, because "394 tests pass and the service publishes nothing" was the
starting state of this phase.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.pdf.null import NullPdf
from app.adapters.tax.vat import VatCalculator
from app.core import events as ev
from app.core.billing_service import BillingService
from app.core.events import Emitter
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)

from tests.unit.test_billing_core import CFG, MockEInv, _plan_sub


def run(c):
    return asyncio.run(c)


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, topic, event):
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def one(self, kind: str):
        found = [e for e in self.events if e.type == kind]
        assert found, f"{kind} was never published — published: {self.types()}"
        return found[0]


class _OkGateway:
    driver = "ok"

    def charge(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="ch_1", status="succeeded")

    def refund(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="rf_1", status="refunded")

    def verify_webhook(self, **kw):
        raise NotImplementedError


class _DeclinedGateway(_OkGateway):
    driver = "declined"

    def charge(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=False, provider_id="ch_x", status="declined")


def _svc(st, gw, bus):
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=gw, tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config=CFG, emitter=Emitter(bus))


def test_a_successful_charge_publishes_payment_succeeded():
    """The whole point of the phase: something happened and a consumer can now hear about it."""
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))

    e = bus.one(ev.PAYMENT_SUCCEEDED)
    assert e.payload["tenant"] == "t1"
    assert e.payload["amount_minor"] > 0
    assert e.payload["payment_id"], "no payment identity: a consumer cannot de-duplicate"


def test_the_renewal_is_published_with_a_period_key_a_consumer_can_key_on():
    """A consumer granting a plan's included allowance keys on the period, not the payment — so a
    replay of the same period cannot grant twice."""
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))

    e = bus.one(ev.SUBSCRIPTION_RENEWED)
    assert e.payload["subscription_id"] == sub.id
    assert e.payload["period_key"], "nothing for a consumer to key idempotency on"


def test_replaying_the_SAME_period_does_not_publish_a_second_renewal():
    """A consumer granting a plan's allowance must not be told twice about one period.

    The period has to be wound back deliberately: run_cycle advances it, so calling it twice in a
    row bills two consecutive periods, which is correct and is NOT a replay. The replay that
    matters is the same period arriving twice — a retry, a duplicate scheduler tick — and that is
    what the deterministic idempotency key guards. The event must sit behind that guard, not in
    front of it.
    """
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))
    first = bus.types().count(ev.SUBSCRIPTION_RENEWED)

    replayed = run(SubscriptionStore(st).fetch(sub.id))
    replayed.current_period_end = replayed.current_period_start   # the key the first cycle used
    run(SubscriptionStore(st).save(replayed))

    run(b.run_cycle(sub.id))
    assert bus.types().count(ev.SUBSCRIPTION_RENEWED) == first,         "the same period was announced twice; a consumer would grant its credits twice"


def test_billing_two_consecutive_periods_publishes_two_renewals():
    """The counterpart, so the guard above cannot pass by suppressing everything."""
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))
    run(b.run_cycle(sub.id))          # the period advanced: this is the NEXT period, not a replay
    assert bus.types().count(ev.SUBSCRIPTION_RENEWED) == 2


def test_a_declined_charge_publishes_payment_failed_and_not_succeeded():
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _DeclinedGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))

    assert ev.PAYMENT_SUCCEEDED not in bus.types(), "a declined charge reported success"
    e = bus.one(ev.PAYMENT_FAILED)
    # A consumer that treats every failure as final dunning-mails a customer whose retry would
    # have succeeded an hour later.
    assert e.payload["final"] is False
    assert e.payload["attempts"] >= 1


def test_a_refund_is_published_so_a_consumer_can_take_back_what_it_granted():
    """Without this, a refunded customer keeps whatever the payment bought."""
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)
    run(b.run_cycle(sub.id))
    rows, _ = run(st.find("payments", {"tenant": "t1"}, limit=10))
    run(b.refund(rows[0]["id"]))
    e = bus.one(ev.PAYMENT_REFUNDED)
    assert e.payload["tenant"] == "t1"


def test_a_cancellation_says_whether_access_ends_now_or_at_the_period_end():
    """A consumer that cannot tell the two apart either cuts access early or never cuts it."""
    st, bus = MemoryDataStore(), _Bus()
    b = _svc(st, _OkGateway(), bus)
    _, sub = _plan_sub(b)

    run(b.cancel(sub.id, at_period_end=True))
    assert bus.one(ev.SUBSCRIPTION_CANCELLED).payload["at_period_end"] is True

    bus.events.clear()
    run(b.cancel(sub.id, at_period_end=False))
    assert bus.one(ev.SUBSCRIPTION_CANCELLED).payload["at_period_end"] is False


def test_a_service_with_no_bus_still_takes_money():
    """Publishing is a side channel. It must never be load-bearing for the payment itself."""
    st = MemoryDataStore()
    b = BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_OkGateway(), tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config=CFG)                     # no emitter at all
    _, sub = _plan_sub(b)
    assert run(b.run_cycle(sub.id)).status == "paid"
