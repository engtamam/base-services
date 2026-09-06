"""PAYMENT_RENEWAL_GATEWAY_MODE=pinned — a subscription's renewals charge its OWN gateway.

The safety invariant: after the active driver is switched, an existing subscriber's renewal must
reach the gateway that ISSUED its token — never the newly-active one, and never silently a different
one when its own gateway cannot build (that fails closed, retryable by dunning). Default `active`
mode is unchanged.
"""
from __future__ import annotations

import asyncio

from app.container import build_named_gateway  # noqa: F401 (import proves the seam exists)
from app.core.billing_service import BillingService
from app.core.models import Invoice, Subscription
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.ports.payment_gateway import Charge, GatewayUnavailable

from tests.unit.test_billing_core import CFG, MockEInv, NullPdf


def run(c):
    return asyncio.run(c)


class _GW:
    def __init__(self, driver, ok=True):
        self.driver = driver
        self.ok = ok
        self.charged = []

    def charge(self, **k):
        self.charged.append(k)
        return Charge(ok=self.ok, status="succeeded" if self.ok else "failed", provider=self.driver,
                      provider_id=f"{self.driver}_ch", amount_minor=k["amount_minor"],
                      currency=k["currency"], error="" if self.ok else "declined")

    def refund(self, **k):
        self.charged.append({"refund": True, **k})
        return Charge(ok=True, status="refunded", provider=self.driver, provider_id="re_1")


class _FailGW:
    """A pinned gateway that will not build — its charge raises, like _UnconfiguredGateway."""
    driver = "broken"

    def charge(self, **k):
        raise GatewayUnavailable("gateway 'broken' not configured")


def _svc(st, active_gw, *, mode="pinned", registry=None):
    registry = registry or {}

    async def resolver(tenant, driver):
        return registry.get(driver, _FailGW())

    cfg = dict(CFG, renewal_gateway_mode=mode)
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=active_gw,
                          tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(), config=cfg,
                          gateway_resolver=resolver)


def _inv(tenant="t1"):
    return Invoice(id="inv_x", tenant=tenant, number="INV-1", subscription_id="sub_x",
                   subtotal_minor=10000, tax_minor=1500, total_minor=11500, currency="SAR",
                   status="open", created="2026-01-01")


# ── stamping ──────────────────────────────────────────────────────────────────
def test_subscribe_pins_the_active_gateway():
    st = MemoryDataStore()
    b = _svc(st, _GW("moyasar"))
    plan = run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    assert sub.payment_gateway == "moyasar"


def test_update_payment_method_repins_to_the_current_active_gateway():
    st = MemoryDataStore()
    b = _svc(st, _GW("moyasar"))
    plan = run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    # the platform later switches the active gateway to stripe; the subscriber re-enters payment
    b2 = _svc(st, _GW("stripe"))
    updated = run(b2.update_payment_method(sub.id, "pm_new"))
    assert updated.payment_gateway == "stripe"       # moved only by their own re-tokenisation


# ── renewal routing ─────────────────────────────────────────────────────────────
def test_renewal_charges_the_subs_pinned_gateway_not_the_active_one():
    st = MemoryDataStore()
    old, new = _GW("moyasar"), _GW("stripe")
    # active is now stripe, but this sub is pinned to moyasar; its renewal must hit moyasar.
    b = _svc(st, new, registry={"moyasar": old, "stripe": new})
    sub = Subscription(id="sub_x", tenant="t1", plan_id="p", status="active",
                       payment_token="pm_moyasar", payment_gateway="moyasar")
    run(b._charge_invoice(_inv(), sub.payment_token, sub))
    assert len(old.charged) == 1 and len(new.charged) == 0     # charged the PINNED gateway


def test_a_pinned_gateway_that_cannot_build_fails_closed_never_the_active():
    st = MemoryDataStore()
    active = _GW("stripe")
    # sub pinned to a driver the resolver cannot build -> _FailGW -> GatewayUnavailable -> dunning.
    b = _svc(st, active, registry={"stripe": active})       # 'moyasar' absent → _FailGW
    sub = Subscription(id="sub_x", tenant="t1", plan_id="p", status="active",
                       payment_token="pm_moyasar", payment_gateway="moyasar")
    run(b._charge_invoice(_inv(), sub.payment_token, sub))
    assert active.charged == []                              # the ACTIVE gateway was NEVER charged
    pays, _ = run(st.find("payments", {"tenant": "t1"}, limit=10))
    assert pays and pays[0]["status"] == "failed"           # failed → retryable by dunning


def test_active_mode_ignores_the_pin_and_uses_the_active_gateway():
    st = MemoryDataStore()
    active = _GW("stripe")
    b = _svc(st, active, mode="active", registry={"moyasar": _GW("moyasar")})
    sub = Subscription(id="sub_x", tenant="t1", plan_id="p", status="active",
                       payment_token="pm", payment_gateway="moyasar")
    run(b._charge_invoice(_inv(), sub.payment_token, sub))
    assert len(active.charged) == 1                          # active mode → the active gateway


# ── refund routing ──────────────────────────────────────────────────────────────
def test_refund_routes_to_the_gateway_that_charged():
    st = MemoryDataStore()
    old, new = _GW("moyasar"), _GW("stripe")
    b = _svc(st, new, registry={"moyasar": old, "stripe": new})
    sub = Subscription(id="sub_x", tenant="t1", plan_id="p", status="active",
                       payment_token="pm", payment_gateway="moyasar")
    run(b._charge_invoice(_inv(), sub.payment_token, sub))   # charged moyasar → pay.provider=moyasar
    pay = run(st.find("payments", {"tenant": "t1"}, limit=10))[0][0]
    run(b.refund(pay["id"]))
    assert any(c.get("refund") for c in old.charged)         # refunded via moyasar, not stripe
    assert not any(c.get("refund") for c in new.charged)
