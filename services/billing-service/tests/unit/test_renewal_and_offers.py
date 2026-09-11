"""The renewal sweep, and the public offers read.

Two gaps in code that was already half-built: run_cycle and SubscriptionStore.due() both existed
and nothing swept on a schedule, so year-two revenue never invoiced; and coupons could be created
but a customer app could not read a public campaign, forcing a mirror that drifts.

The tests that matter: the sweep must invoice ONLY what is due (run_cycle charges unconditionally,
so the filter is the only guard against charging everyone daily), and the offers read must never
leak a private code or an operator-only field.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.pdf.null import NullPdf
from app.adapters.tax.vat import VatCalculator
from app.core import coupons as C
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)

from tests.unit.test_billing_core import CFG, MockEInv

NOW = 2_000_000.0


def run(c):
    return asyncio.run(c)


class _Gw:
    driver = "ok"

    def charge(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="ch_1", status="succeeded")

    def refund(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="rf", status="refunded")

    def verify_webhook(self, **kw):
        raise NotImplementedError


def _svc(st, now=NOW, cfg=None):
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=_Gw(), tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(),
        config={**CFG, **(cfg or {})}, clock=lambda: now)


def _plan(b):
    return run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}]))


def _sub_due(b, st, tenant, period_end):
    """A subscription whose current period ended at `period_end`."""
    plan = _plan(b)
    sub = run(b.subscribe(tenant=tenant, plan_id=plan.id, payment_token="pm"))
    sub.current_period_end = period_end
    run(SubscriptionStore(st).save(sub))
    return sub


# ── the renewal sweep ─────────────────────────────────────────────────────────
def test_the_sweep_invoices_a_subscription_whose_period_has_ended():
    """The whole point: year-two revenue that never invoiced now does."""
    st = MemoryDataStore()
    b = _svc(st)
    sub = _sub_due(b, st, "acme", NOW - 10)
    before = len(run(InvoiceStore(st).list("acme", 100)))
    report = run(b.cycle_due_sweep())
    assert report["invoiced"] == 1
    assert len(run(InvoiceStore(st).list("acme", 100))) == before + 1


def test_the_sweep_does_NOT_invoice_a_period_that_has_not_ended():
    """run_cycle charges unconditionally, so the <= now filter is the only thing between 'renew
    what is due' and 'charge everyone today'. This is the most important test in the file."""
    st = MemoryDataStore()
    b = _svc(st)
    _sub_due(b, st, "acme", NOW + 100_000)      # period ends in the future
    report = run(b.cycle_due_sweep())
    assert report["due"] == 0 and report["invoiced"] == 0


def test_the_lead_window_invoices_ahead_of_the_boundary():
    """An operator may raise a renewal before access lapses."""
    st = MemoryDataStore()
    b = _svc(st, cfg={"cycle_lead_seconds": 100_000})
    _sub_due(b, st, "acme", NOW + 50_000)       # ends within the lead window
    assert run(b.cycle_due_sweep())["invoiced"] == 1


def test_a_re_run_charges_nobody_twice():
    """run_cycle is idempotent per cycle:{sub}:{period_end}, so a partial sweep re-run is safe."""
    st = MemoryDataStore()
    b = _svc(st)
    _sub_due(b, st, "acme", NOW - 10)
    run(b.cycle_due_sweep())
    invoices_after_first = len(run(InvoiceStore(st).list("acme", 100)))
    run(b.cycle_due_sweep())
    assert len(run(InvoiceStore(st).list("acme", 100))) == invoices_after_first


class _GwRaisesFor:
    """A gateway that throws a non-GatewayUnavailable error for one token — a genuinely broken sub,
    the kind that would abort a naive sweep."""

    driver = "flaky"

    def __init__(self, bad_token):
        self._bad = bad_token

    def charge(self, *, token, **kw):
        if token == self._bad:
            raise RuntimeError("provider exploded")
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="ch", status="succeeded")

    def refund(self, **kw):
        from app.ports.payment_gateway import Charge
        return Charge(ok=True, provider_id="r", status="refunded")

    def verify_webhook(self, **kw):
        raise NotImplementedError


def test_one_unbillable_subscription_does_not_stop_the_others():
    """A provider error on one sub must not deny renewal to a thousand others — each is tried in
    isolation and its failure recorded, not raised."""
    st = MemoryDataStore()
    b = _svc(st)
    b._gw = _GwRaisesFor("bad_token")
    # good sub (default token "pm"), and a broken one whose charge throws.
    good = _sub_due(b, st, "acme", NOW - 10)
    plan = _plan(b)
    bad = run(b.subscribe(tenant="globex", plan_id=plan.id, payment_token="bad_token"))
    bad.current_period_end = NOW - 10
    run(SubscriptionStore(st).save(bad))

    report = run(b.cycle_due_sweep())
    assert report["invoiced"] >= 1 and report["failed"] >= 1
    assert any(e["subscription_id"] == bad.id for e in report["errors"])
    assert run(InvoiceStore(st).list("acme", 100)), "the good sub was not invoiced"


def test_the_pass_is_bounded_and_reports_when_capped():
    st = MemoryDataStore()
    b = _svc(st, cfg={"cycle_sweep_limit": 1})
    _sub_due(b, st, "acme", NOW - 10)
    _sub_due(b, st, "globex", NOW - 20)
    report = run(b.cycle_due_sweep())
    assert report["invoiced"] == 1 and report["capped"] is True


def test_a_cancelled_subscription_is_not_renewed():
    st = MemoryDataStore()
    b = _svc(st)
    sub = _sub_due(b, st, "acme", NOW - 10)
    sub.status = "canceled"
    run(SubscriptionStore(st).save(sub))
    assert run(b.cycle_due_sweep())["due"] == 0


def test_cancel_at_period_end_is_finalized_not_renewed():
    """The reported money bug: cancel(at_period_end=True) set the flag but the sweep still renewed and
    charged. At the boundary the sub must be FINALIZED (canceled), NO invoice, NO charge."""
    st = MemoryDataStore()
    b = _svc(st)
    sub = _sub_due(b, st, "acme", NOW - 10)
    run(b.cancel(sub.id, at_period_end=True))              # flag set, status still active
    before = len(run(InvoiceStore(st).list("acme", 100)))
    report = run(b.cycle_due_sweep())
    assert report["invoiced"] == 0 and report["canceled"] == 1
    assert len(run(InvoiceStore(st).list("acme", 100))) == before   # NO invoice cut
    after = run(SubscriptionStore(st).fetch(sub.id))
    assert after.status == "canceled" and after.canceled_at        # finalized + stamped


def test_run_cycle_directly_finalizes_a_cancel_at_period_end_sub():
    st = MemoryDataStore()
    b = _svc(st)
    sub = _sub_due(b, st, "acme", NOW - 10)
    run(b.cancel(sub.id, at_period_end=True))
    inv = run(b.run_cycle(sub.id))
    assert inv.id == ""                                    # the no-invoice sentinel — nothing billed
    assert run(SubscriptionStore(st).fetch(sub.id)).status == "canceled"
    # idempotent: a re-run refuses (already canceled), never charges
    import pytest
    with pytest.raises(ValueError):
        run(b.run_cycle(sub.id))


def test_consolidated_cycle_finalizes_a_cancel_at_period_end_sub_not_bills_it():
    st = MemoryDataStore()
    b = _svc(st, cfg={"renewal_invoice_mode": "consolidated"})
    keep = _sub_due(b, st, "acme", NOW - 10)
    stop = _sub_due(b, st, "acme", NOW - 10)
    run(b.cancel(stop.id, at_period_end=True))
    run(b.cycle_due_sweep())
    assert run(SubscriptionStore(st).fetch(stop.id)).status == "canceled"   # finalized, not billed
    assert run(SubscriptionStore(st).fetch(keep.id)).status == "active"     # the other still renews
    # the consolidated invoice has a line only for the kept sub, never the canceled one
    invs = run(InvoiceStore(st).list("acme", 100))
    assert sum(len(i["lines"]) for i in invs) == 1


# ── public offers ─────────────────────────────────────────────────────────────
def test_a_public_active_coupon_appears_as_an_offer():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SUMMER", type="percent", value=15, public=True))
    offers = run(b.public_offers())
    assert [o["code"] for o in offers] == ["SUMMER"]
    assert offers[0]["value"] == 15 and offers[0]["type"] == "percent"


def test_a_private_coupon_is_NEVER_offered():
    """A targeted or partner code must not leak just because it is active — the default is private."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="VIP50", type="percent", value=50))   # public defaults False
    assert run(b.public_offers()) == []


def test_an_expired_public_coupon_is_not_offered():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="GONE", type="percent", value=10, public=True, expires_at=NOW - 1))
    assert run(b.public_offers()) == []


def test_a_disabled_public_coupon_is_not_offered():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="OFF", type="percent", value=10, public=True, active=False))
    assert run(b.public_offers()) == []


def test_offers_never_expose_operator_only_fields():
    """max_redemptions, redeemed and the operator note are not the customer's business."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="SEEN", type="percent", value=10, public=True,
                        max_redemptions=100, note="partner deal Q3"))
    run(CouponStore(st).redeem("SEEN"))
    offer = run(b.public_offers())[0]
    for banned in ("max_redemptions", "redeemed", "note", "active", "public"):
        assert banned not in offer
    assert set(offer) <= set(b.PUBLIC_OFFER_FIELDS)


def test_a_coupon_can_be_flipped_public_and_back():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert_coupon(code="FLIP", type="percent", value=10))
    assert run(b.public_offers()) == []
    run(b.update_coupon("FLIP", public=True))
    assert [o["code"] for o in run(b.public_offers())] == ["FLIP"]
    run(b.update_coupon("FLIP", public=False))
    assert run(b.public_offers()) == []
