"""Editing a live plan.

The load-bearing test is that nobody already on the plan is repriced. Everything else here is
about a flag that existed on the model and that nothing read: `active` could be set to False and
the service kept selling the plan, which is worse than not having the flag, because an operator
believed they had retired it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.pdf.null import NullPdf
from app.adapters.tax.vat import VatCalculator
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)

from tests.unit.test_billing_core import CFG, MockEInv


def run(c):
    return asyncio.run(c)


class _Gw:
    driver = "ok"

    def charge(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="ch_1", status="succeeded")

    def refund(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="rf_1", status="refunded")

    def verify_webhook(self, **kw):
        raise NotImplementedError


def _svc(st, cfg=None):
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_Gw(), tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config={**CFG, **(cfg or {})})


def _plan(b, amount=10000):
    return run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": amount,
                                                  "currency": "SAR", "type": "flat"}],
                             limits={"seats": 5}))


# ── the one that matters ──────────────────────────────────────────────────────
def test_repricing_a_plan_does_not_reprice_an_existing_subscriber():
    """Silently changing what someone already agreed to pay is the failure customers escalate
    rather than report."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b, amount=10000)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))

    run(b.update_plan(plan.id, prices=[{"interval": "month", "amount_minor": 99000,
                                        "currency": "SAR", "type": "flat"}]))
    inv = run(b.run_cycle(sub.id))
    assert inv.subtotal_minor < 99000, "an existing subscriber was billed the NEW price"


def test_a_new_subscriber_gets_the_new_price():
    """The counterpart, so the guard above cannot pass by the update doing nothing at all."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b, amount=10000)
    run(b.update_plan(plan.id, prices=[{"interval": "month", "amount_minor": 99000,
                                        "currency": "SAR", "type": "flat"}]))
    sub = run(b.subscribe(tenant="t2", plan_id=plan.id, payment_token="pm_1"))
    assert sub.price_snapshot["amount_minor"] == 99000


def test_a_platform_can_choose_to_reprice_immediately():
    """This is a general platform: an adopter whose plans are metered utilities wants the current
    rate to be the rate. That is a config choice, not a behaviour baked in."""
    st = MemoryDataStore()
    b = _svc(st, {"grandfather": False})
    plan = _plan(b, amount=10000)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    run(b.update_plan(plan.id, prices=[{"interval": "month", "amount_minor": 20000,
                                        "currency": "SAR", "type": "flat"}]))
    assert run(b.run_cycle(sub.id)).subtotal_minor > 10000


# ── retiring ──────────────────────────────────────────────────────────────────
def test_a_retired_plan_cannot_be_subscribed_to():
    """`active` existed on the model and nothing read it, so an operator could retire a plan and
    the service kept selling it."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.set_plan_availability(plan.id, active=False))
    with pytest.raises(ValueError, match="PLAN_NOT_AVAILABLE"):
        run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))


def test_retiring_a_plan_does_not_disturb_existing_subscribers():
    """They bought terms. Retiring a plan is not a reason to stop honouring them."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1"))
    run(b.set_plan_availability(plan.id, active=False))
    assert run(b.run_cycle(sub.id)).status == "paid"


def test_a_retired_plan_disappears_from_the_public_list_but_not_the_admin_one():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.set_plan_availability(plan.id, active=False))
    assert plan.id not in [p["id"] for p in run(b.list_plans())]
    assert plan.id in [p["id"] for p in run(b.list_plans(include_hidden=True))]


def test_an_unlisted_plan_is_hidden_but_still_sellable():
    """How a legacy or negotiated plan keeps working without being on the pricing page."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    run(b.set_plan_availability(plan.id, listed=False))
    assert plan.id not in [p["id"] for p in run(b.list_plans())]
    assert run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm_1")) is not None


# ── refusals ──────────────────────────────────────────────────────────────────
def test_editing_an_unknown_field_is_refused_rather_than_ignored():
    """A field silently dropped is how an operator comes to believe they changed something they
    did not."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    with pytest.raises(ValueError, match="NOT_EDITABLE"):
        run(b.update_plan(plan.id, id="plan_other"))


def test_a_plan_cannot_be_left_with_no_price():
    # It would bill zero for ever while the cycle kept running.
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    with pytest.raises(ValueError, match="NEEDS_A_PRICE"):
        run(b.update_plan(plan.id, prices=[]))


def test_updating_an_unknown_plan_is_refused():
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        run(_svc(MemoryDataStore()).update_plan("plan_nope", name="x"))


def test_a_partial_update_leaves_the_other_fields_alone():
    """An operator correcting a price must not have to resend the limits and risk clobbering
    them with whatever the form happened to hold."""
    st = MemoryDataStore()
    b = _svc(st)
    plan = _plan(b)
    updated = run(b.update_plan(plan.id, name="Pro Plus"))
    assert updated.name == "Pro Plus"
    assert updated.limits == {"seats": 5}
    assert updated.prices[0]["amount_minor"] == 10000
