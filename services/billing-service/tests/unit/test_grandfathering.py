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

    def __init__(self, ok=True):
        self.ok = ok
        self.charges = []

    def charge(self, **k):
        self.charges.append(k)
        return Charge(ok=self.ok, status="succeeded" if self.ok else "failed", provider="mock",
                      provider_id="ch", amount_minor=k["amount_minor"], currency=k["currency"],
                      error="" if self.ok else "DECLINED")

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


def _svc(st, cfg=None, gw=None):
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=gw or GW(),
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


# ── Stripe-style plan change: paid upgrade now, scheduled downgrade at renewal, NEVER a credit ────────
def _two_plans(b):
    basic = _setup(b, price=10000, seats=5)
    pro = run(b.create_plan(name="Max", prices=[{"interval": "month", "amount_minor": 30000,
                                                 "currency": "SAR"}], limits={"seats": 20}))
    return basic, pro


def test_upgrade_charges_the_difference_first_then_flips(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); gw = GW(ok=True); b = _svc(st, gw=gw)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)   # whole period remains
    res = run(b.change_plan(sub.id, new_plan_id=pro.id))
    assert res["applied"] == "upgraded" and res["proration_minor"] == 20000 and res["invoice_id"]
    assert gw.charges[-1]["amount_minor"] == 23000                        # 20000 + 15% VAT, charged NOW
    up = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert up.status == "paid" and up.total_minor == 23000
    assert up.lines[0]["description"] == "Plan change — upgrade"
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.plan_id == pro.id and s.limits_snapshot == {"seats": 20}     # flips only after the charge
    assert run(b.run_cycle(sub.id)).subtotal_minor == 30000               # next renewal: pro full price, no leftover


def test_upgrade_decline_changes_nothing():
    from app.core.billing_service import UpgradeChargeFailed
    st = MemoryDataStore(); b = _svc(st, gw=GW(ok=False))
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token="pm"))
    with pytest.raises(UpgradeChargeFailed):
        run(b.change_plan(sub.id, new_plan_id=pro.id))
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.plan_id == basic.id and s.limits_snapshot == {"seats": 5}    # old plan intact
    assert len(run(InvoiceStore(st).list("ali"))) == 0                    # no lingering invoice


def test_upgrade_without_a_card_is_refused(monkeypatch):
    import app.core.billing_service as bs
    from app.core.billing_service import UpgradeChargeFailed
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=basic.id, payment_token=""))   # no card on file
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    with pytest.raises(UpgradeChargeFailed, match="NO_PAYMENT_METHOD"):
        run(b.change_plan(sub.id, new_plan_id=pro.id))
    assert run(SubscriptionStore(st).fetch(sub.id)).plan_id == basic.id


def test_downgrade_is_scheduled_never_credited(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); gw = GW(ok=True); b = _svc(st, gw=gw)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=pro.id, payment_token="pm"))   # start high
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    res = run(b.change_plan(sub.id, new_plan_id=basic.id))
    assert res["applied"] == "scheduled_downgrade" and res["pending_plan_id"] == basic.id
    assert not gw.charges and len(run(InvoiceStore(st).list("ali"))) == 0    # nothing charged, no credit invoice
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.plan_id == pro.id and s.limits_snapshot == {"seats": 20}        # keeps the paid-for plan
    assert s.pending_plan_id == basic.id


def test_scheduled_downgrade_applies_at_full_price_at_renewal(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=pro.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=basic.id))
    inv = run(b.run_cycle(sub.id))
    assert inv.subtotal_minor == 10000                                       # new plan, full price, no proration
    assert all("Plan change" not in l.get("description", "") for l in inv.lines)   # never a credit line
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.plan_id == basic.id and s.limits_snapshot == {"seats": 5} and not s.pending_plan_id


def test_an_upgrade_supersedes_a_scheduled_downgrade(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b)
    mid = run(b.create_plan(name="Mid", prices=[{"interval": "month", "amount_minor": 20000,
                                                 "currency": "SAR"}], limits={"seats": 10}))
    sub = run(b.subscribe(tenant="ali", plan_id=mid.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=basic.id))     # downgrade scheduled (20000 -> 10000)
    assert run(SubscriptionStore(st).fetch(sub.id)).pending_plan_id == basic.id
    run(b.change_plan(sub.id, new_plan_id=pro.id))       # upgrade (20000 -> 30000) supersedes it
    s = run(SubscriptionStore(st).fetch(sub.id))
    assert s.plan_id == pro.id and not s.pending_plan_id


def test_cancel_scheduled_change_undoes_it_and_is_idempotent(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=pro.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=basic.id))
    assert run(b.cancel_scheduled_change(sub.id))["cancelled_scheduled_change"] is True
    assert not run(SubscriptionStore(st).fetch(sub.id)).pending_plan_id
    assert run(b.run_cycle(sub.id)).subtotal_minor == 30000               # stays on pro
    assert run(b.cancel_scheduled_change(sub.id))["cancelled_scheduled_change"] is False   # no-op


def test_cancel_at_period_end_wins_over_a_scheduled_downgrade(monkeypatch):
    import app.core.billing_service as bs
    st = MemoryDataStore(); b = _svc(st)
    basic, pro = _two_plans(b)
    sub = run(b.subscribe(tenant="ali", plan_id=pro.id, payment_token="pm"))
    monkeypatch.setattr(bs, "now_ts", lambda: sub.current_period_start)
    run(b.change_plan(sub.id, new_plan_id=basic.id))     # downgrade scheduled
    run(b.cancel(sub.id, at_period_end=True))            # and then a cancel
    inv = run(b.run_cycle(sub.id))
    assert run(SubscriptionStore(st).fetch(sub.id)).plan_id == pro.id     # downgrade NOT adopted
    assert inv.subtotal_minor == 30000
