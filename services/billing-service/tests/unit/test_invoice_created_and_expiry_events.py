"""invoice.created + subscription.expiring_soon — the two bus events a consumer uses for in-app
awaiting-payment and pre-expiry prompts. These drive the REAL service (run_cycle / run_overage /
run_consolidated_cycle / expiry_warn_sweep) and assert on what actually came out of the bus, not just
the emitter envelope.
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

from tests.unit.test_billing_core import CFG, MockEInv


def run(c):
    return asyncio.run(c)


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, topic, event):
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, kind: str) -> list:
        return [e for e in self.events if e.type == kind]

    def one(self, kind: str):
        found = self.of(kind)
        assert found, f"{kind} was never published — published: {self.types()}"
        assert len(found) == 1, f"{kind} published {len(found)}× — expected once: {self.types()}"
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


DAY = 86400


class _Clock:
    """A movable clock, so expiry windows are deterministic."""
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _svc(st, bus, clock, **cfg_over):
    cfg = dict(CFG)
    cfg.update(cfg_over)
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_OkGateway(), tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config=cfg, emitter=Emitter(bus), clock=clock)


def _monthly_plan(b):
    return run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}]))


def _subscribe(b, tenant="t1", token="pm_1"):
    plan = _monthly_plan(b)
    sub = run(b.subscribe(tenant=tenant, plan_id=plan.id, payment_token=token))
    return plan, sub


# ── invoice.created ───────────────────────────────────────────────────────────
def test_invoice_created_fires_on_renewal_before_the_charge():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk)
    plan, sub = _subscribe(b)
    bus.events.clear()
    run(b.run_cycle(sub.id))

    created = bus.one(ev.INVOICE_CREATED)
    p = created.payload
    assert p["subscription_id"] == sub.id and p["plan_id"] == sub.plan_id
    assert p["tenant"] == "t1" and p["amount_minor"] > 0 and p["currency"] == "SAR"
    assert p["invoice_id"] and p["due_at"]
    assert p["period_start"] and p["period_end"]
    # BEFORE the money: invoice.created must precede invoice.paid / payment.succeeded on the bus.
    order = bus.types()
    assert order.index(ev.INVOICE_CREATED) < order.index(ev.PAYMENT_SUCCEEDED)
    assert order.index(ev.INVOICE_CREATED) < order.index(ev.INVOICE_PAID)


def test_invoice_created_payload_carries_no_buyer_pii():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk)
    _, sub = _subscribe(b)
    bus.events.clear()
    run(b.run_cycle(sub.id))
    p = bus.one(ev.INVOICE_CREATED).payload
    assert set(p) == {"tenant", "subscription_id", "invoice_id", "plan_id",
                      "amount_minor", "currency", "due_at", "period_start", "period_end"}


def test_invoice_created_does_not_refire_on_cycle_replay():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk)
    _, sub = _subscribe(b)
    bus.events.clear()
    run(b.run_cycle(sub.id))
    # A replay of the SAME period (idempotency claim already marked) must re-cut nothing and re-emit
    # nothing — invoice.created fired exactly once, on the fresh create.
    first = len(bus.of(ev.INVOICE_CREATED))
    # Re-run the same period by rewinding the sub one interval so the cycle key matches the marked one.
    s = run(b._subs.fetch(sub.id))
    import app.core.billing_service as bs
    s.current_period_start -= bs._INTERVAL["month"]
    s.current_period_end -= bs._INTERVAL["month"]
    run(b._subs.save(s))
    run(b.run_cycle(sub.id))
    assert len(bus.of(ev.INVOICE_CREATED)) == first == 1


def test_invoice_created_fires_once_for_a_consolidated_invoice():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, renewal_invoice_mode="consolidated")
    p1 = _monthly_plan(b)
    s1 = run(b.subscribe(tenant="t1", plan_id=p1.id, payment_token="pm_1"))
    s2 = run(b.subscribe(tenant="t1", plan_id=p1.id, payment_token="pm_1"))
    bus.events.clear()
    inv = run(b.run_consolidated_cycle([s1, s2]))
    assert inv is not None
    created = bus.one(ev.INVOICE_CREATED)          # exactly ONE, not one per subscription
    assert created.payload["consolidated"] is True
    assert created.payload["subscription_id"] == "" and created.payload["plan_id"] == ""
    assert created.payload["invoice_id"] == inv.id


# ── subscription.expiring_soon ────────────────────────────────────────────────
def _set_period_end(b, sub_id, period_end):
    s = run(b._subs.fetch(sub_id))
    s.current_period_end = period_end
    run(b._subs.save(s))
    return s


def test_expiring_soon_fires_inside_the_window_with_days_remaining():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=3)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t + 2 * DAY + 3600)   # ~2.04 days out → inside 3-day window
    bus.events.clear()
    res = run(b.expiry_warn_sweep())
    assert res["warned"] == 1
    p = bus.one(ev.SUBSCRIPTION_EXPIRING_SOON).payload
    assert p["subscription_id"] == sub.id and p["plan_id"] == sub.plan_id and p["tenant"] == "t1"
    assert p["days_remaining"] == 3           # 2.04 days rounds UP to 3, never 0/2


def test_expiring_soon_fires_exactly_once_per_period():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=3)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t + DAY)
    run(b.expiry_warn_sweep())
    before = len(bus.of(ev.SUBSCRIPTION_EXPIRING_SOON))
    run(b.expiry_warn_sweep())                 # a duplicate tick must warn nobody again
    assert before == 1
    assert len(bus.of(ev.SUBSCRIPTION_EXPIRING_SOON)) == 1


def test_expiring_soon_rearms_after_the_period_rolls():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=3)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t + DAY)
    run(b.expiry_warn_sweep())
    # Period rolls forward (as a renewal would) → a NEW (sub, period_end) key → warned afresh.
    _set_period_end(b, sub.id, clk.t + 31 * DAY)        # next period, outside window now
    run(b.expiry_warn_sweep())
    assert len(bus.of(ev.SUBSCRIPTION_EXPIRING_SOON)) == 1   # new period not yet in window
    clk.t += 29 * DAY                                   # advance until the new period is ~2 days out
    run(b.expiry_warn_sweep())
    assert len(bus.of(ev.SUBSCRIPTION_EXPIRING_SOON)) == 2   # re-armed, warned for the new period


def test_expiring_soon_skips_a_period_beyond_the_window():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=3)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t + 10 * DAY)        # well outside 3 days
    res = run(b.expiry_warn_sweep())
    assert res["warned"] == 0 and bus.of(ev.SUBSCRIPTION_EXPIRING_SOON) == []


def test_expiring_soon_skips_an_already_lapsed_period():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=3)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t - DAY)             # already ended — renewal's job, not a warning
    res = run(b.expiry_warn_sweep())
    assert res["warned"] == 0 and bus.of(ev.SUBSCRIPTION_EXPIRING_SOON) == []


def test_expiry_warn_days_zero_disables_warning():
    st, bus, clk = MemoryDataStore(), _Bus(), _Clock()
    b = _svc(st, bus, clk, expiry_warn_days=0)
    _, sub = _subscribe(b)
    _set_period_end(b, sub.id, clk.t + DAY)
    res = run(b.expiry_warn_sweep())
    assert res.get("disabled") is True and res["warned"] == 0
    assert bus.of(ev.SUBSCRIPTION_EXPIRING_SOON) == []


def test_expiry_warn_sweep_is_a_noop_without_an_event_bus():
    st, clk = MemoryDataStore(), _Clock()
    b = BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_OkGateway(), tax=VatCalculator("0.15"), einvoicer=None, pdf=NullPdf(),
        config=dict(CFG), emitter=None, clock=clk)   # no emitter wired
    res = run(b.expiry_warn_sweep())
    assert res["warned"] == 0 and res.get("disabled") is True
