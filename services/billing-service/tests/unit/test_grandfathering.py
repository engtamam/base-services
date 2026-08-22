"""Grandfathering: editing the plan catalogue never silently re-prices/re-limits an
existing customer. Only an explicit migrate() moves them onto current terms."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge, WebhookEvent

CFG = {"cycle_sweep_limit": 1000, "platform_max_pages": 50, "cycle_lead_seconds": 0, "grant_fund": "credits", "plan_credit_limit_key": "credits",
       "plan_credit_grant_mode": "accumulate",
       "currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "dunning_base_hours": 24,
       "dunning_max_attempts": 3, "grace_days": 7, "list_limit": 100, "reverse_charge": False,
       "grandfather": True,
       # Phase 42: required by EntitlementResolver.
       "billable_statuses": ("active", "trialing", "past_due"), "grandfather": True, "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}, "period_tolerance_seconds": 86400,
}


def run(c):
    return asyncio.run(c)


class GW:
    driver = "mock"

    def charge(self, **k):
        return Charge(ok=True, status="succeeded", provider="mock", provider_id="ch",
                      amount_minor=k["amount_minor"], currency=k["currency"])

    def refund(self, **k):
        return Charge(ok=True, status="refunded", provider="mock")

    def verify_webhook(self, **k):
        return WebhookEvent(verified=False)


class EInv:
    driver = "z"

    def finalize(self, *, tenant, invoice):
        return {"status": "reported"}


class NullPdf:
    driver = "null"

    def render(self, i):
        return None


def _svc(st, cfg=None):
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=GW(),
                          tax=VatCalculator("0.15"), einvoicer=EInv(), pdf=NullPdf(), config=cfg or CFG)


def _setup(b, price=10000, seats=5):
    return run(b.create_plan(name="Pro",
                             prices=[{"interval": "month", "amount_minor": price, "currency": "SAR"}],
                             limits={"seats": seats}))


async def _reprice(b, plan_id, price, seats):
    """Simulate the operator editing the live catalogue."""
    plan = await b._plans.fetch(plan_id)
    plan.prices = [{"interval": "month", "amount_minor": price, "currency": "SAR"}]
    plan.limits = {"seats": seats}
    await b._plans.create(plan)


def test_snapshot_captured_at_signup():
    b = _svc(MemoryDataStore())
    plan = _setup(b)
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    assert sub.price_snapshot["amount_minor"] == 10000
    assert sub.limits_snapshot == {"seats": 5} and sub.terms_version


def test_price_rise_does_not_touch_existing_customer():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _setup(b, price=10000, seats=5)
    old = run(b.subscribe(tenant="ali", plan_id=plan.id, payment_token="pm"))
    run(_reprice(b, plan.id, 15000, 3))                    # operator raises price, cuts seats

    inv = run(b.run_cycle(old.id))
    assert inv.subtotal_minor == 10000                      # still the old price
    assert inv.total_minor == 11500

    # a NEW customer pays the new price
    new = run(b.subscribe(tenant="sara", plan_id=plan.id, payment_token="pm"))
    assert run(b.run_cycle(new.id)).subtotal_minor == 15000


def test_grandfathered_limits_survive_a_catalogue_cut():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _setup(b, seats=5)
    run(b.subscribe(tenant="ali", plan_id=plan.id, payment_token="pm"))
    run(_reprice(b, plan.id, 15000, 3))                    # seats cut to 3
    # Ali keeps 5 seats — his 4th/5th users are NOT locked out
    assert run(b.check_entitlement(tenant="ali", key="seats", current=4))["allowed"] is True
    assert run(b.check_entitlement(tenant="ali", key="seats", current=5))["allowed"] is False


def test_migrate_is_explicit_and_moves_to_current_terms():
    st = MemoryDataStore()
    b = _svc(st)
    plan = _setup(b, price=10000, seats=5)
    sub = run(b.subscribe(tenant="ali", plan_id=plan.id, payment_token="pm"))
    run(_reprice(b, plan.id, 15000, 3))

    res = run(b.migrate_terms(sub.id))
    assert res["old_amount_minor"] == 10000 and res["new_amount_minor"] == 15000
    assert res["limits"] == {"seats": 3}
    assert run(b.run_cycle(sub.id)).subtotal_minor == 15000        # now on new price
    assert run(b.check_entitlement(tenant="ali", key="seats", current=3))["allowed"] is False


def test_migrate_unknown_subscription():
    b = _svc(MemoryDataStore())
    with pytest.raises(ValueError):
        run(b.migrate_terms("nope"))


def test_grandfather_off_uses_live_catalogue():
    st = MemoryDataStore()
    b = _svc(st, {**CFG, "grandfather": False})
    plan = _setup(b, price=10000, seats=5)
    sub = run(b.subscribe(tenant="ali", plan_id=plan.id, payment_token="pm"))
    run(_reprice(b, plan.id, 15000, 3))
    assert run(b.run_cycle(sub.id)).subtotal_minor == 15000        # live price applies
    assert run(b.check_entitlement(tenant="ali", key="seats", current=3))["allowed"] is False


def test_plan_change_recaptures_terms():
    st = MemoryDataStore()
    b = _svc(st)
    basic = _setup(b, price=10000, seats=5)
    pro = run(b.create_plan(name="Max", prices=[{"interval": "month", "amount_minor": 30000,
                                                 "currency": "SAR"}], limits={"seats": 20}))
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    run(b.change_plan(sub.id, new_plan_id=pro.id))                  # customer chose this
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.price_snapshot["amount_minor"] == 30000 and s.limits_snapshot == {"seats": 20}


# ── change_plan proration is APPLIED, not computed and discarded ──────────────────────────────────────
def _two_plans(b, basic_price=10000, other_price=30000, other_seats=20):
    basic = _setup(b, price=basic_price)
    other = run(b.create_plan(name="Max", prices=[{"interval": "month", "amount_minor": other_price,
                                                    "currency": "SAR"}], limits={"seats": other_seats}))
    return basic, other


def test_upgrade_proration_is_charged_on_the_next_invoice(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b, 10000, 30000)
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)   # whole period remains -> full diff
    res = run(b.change_plan(sub.id, new_plan_id=pro.id))
    assert res["proration_minor"] == 20000 and res["pending_adjustment_minor"] == 20000

    inv = run(b.run_cycle(sub.id))
    assert inv.subtotal_minor == 50000                     # 30000 next period + 20000 upgrade proration
    assert any(l.get("description", "").startswith("Plan change charge") and l["amount_minor"] == 20000
               for l in inv.lines)
    assert inv.tax_minor == 7500                            # VAT applies to the proration too (15% of 50000)
    assert run(SubscriptionStore(st).fetch(sub.id)).pending_adjustment_minor == 0   # consumed


def test_downgrade_proration_credits_the_next_invoice(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, mid = _two_plans(b, 30000, 25000)               # start high, move to a slightly cheaper plan
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    res = run(b.change_plan(sub.id, new_plan_id=mid.id))
    assert res["proration_minor"] == -5000                 # a credit

    inv = run(b.run_cycle(sub.id))
    assert inv.subtotal_minor == 20000                     # 25000 next period − 5000 credit
    assert any(l.get("description", "").startswith("Plan change credit") and l["amount_minor"] == -5000
               for l in inv.lines)
    assert run(SubscriptionStore(st).fetch(sub.id)).pending_adjustment_minor == 0


def test_a_credit_larger_than_one_invoice_carries_forward_and_never_goes_negative(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, cheap = _two_plans(b, 30000, 10000)             # big downgrade -> −20000 credit vs a 10000 plan
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=cheap.id))       # pending = −20000

    inv1 = run(b.run_cycle(sub.id))
    assert inv1.subtotal_minor == 0 and inv1.total_minor == 0    # 10000 fully credited, never negative
    assert run(SubscriptionStore(st).fetch(sub.id)).pending_adjustment_minor == -10000  # remainder carried

    inv2 = run(b.run_cycle(sub.id))                        # next cycle applies the rest
    assert inv2.subtotal_minor == 0
    assert run(SubscriptionStore(st).fetch(sub.id)).pending_adjustment_minor == 0        # fully consumed


def test_two_changes_in_one_period_net_before_billing(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b, 10000, 30000)
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=pro.id))         # +20000
    res = run(b.change_plan(sub.id, new_plan_id=basic.id))  # back down: −20000
    assert res["pending_adjustment_minor"] == 0            # the two net to nothing

    inv = run(b.run_cycle(sub.id))
    assert inv.subtotal_minor == 10000                     # just the (basic) plan; no stray adjustment line
    assert not any("Plan change" in l.get("description", "") for l in inv.lines)
