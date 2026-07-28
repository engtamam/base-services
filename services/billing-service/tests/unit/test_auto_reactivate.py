"""Auto-reactivate a paused subscription the instant its invoice is paid.

_mark_paid is the ONE place an invoice becomes paid — a card charge, a webhook reconcile and an
off-gateway settlement all converge there — so hooking reactivation there means it fires from EVERY
paid path, once, and only paused -> active.
"""
from __future__ import annotations

import asyncio
import time

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.models import Invoice, Subscription, new_id
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _Einvoicer:
    def finalize(self, *, tenant, invoice):
        return {"uuid": "e"}


def _svc(**over):
    st = MemoryDataStore()
    cfg = dict(currency="SAR", trial_days=0, vat_rate="0.15", reverse_charge=False, grandfather=True,
               dunning_base_hours=24, dunning_max_attempts=4, grace_days=7, list_limit=100,
               limits_cache_ttl=300, settlement_methods={"bank_transfer": True},
               settlement_allow_partial=True, settlement_tolerance_minor=0,
               settlement_max_backdate_days=90, billable_statuses=("active", "trialing", "past_due"),
               max_subscriptions=200, merge_strategies={}, rank_orders={})
    cfg.update(over)
    svc = BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                         invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                         ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=None, tax=None,
                         einvoicer=_Einvoicer(), pdf=None, config=cfg, settlements=SettlementStore(st))
    return svc


async def _setup(svc, *, sub_status="paused", sid="sub_1", tenant="acme"):
    sub = Subscription(id=sid, tenant=tenant, plan_id="p1", status=sub_status)
    await svc._subs.save(sub)
    inv = Invoice(id=new_id("inv_"), tenant=tenant, number="INV-1", total_minor=100_000,
                  currency="SAR", status="open", subscription_id=sid, created="2026-01-01T00:00:00Z")
    await svc._invoices.save(inv)
    return sub, inv


def _settle(svc, inv):
    return run(svc.settle(invoice_id=inv.id, method="bank_transfer", issuer="SABB",
                          reference="MT-1", amount_minor=inv.total_minor, paid_at=time.time()))


def test_paying_a_paused_subscriptions_invoice_reactivates_it():
    svc = _svc()
    _, inv = run(_setup(svc, sub_status="paused"))
    _settle(svc, inv)
    assert run(svc._subs.fetch("sub_1")).status == "active"


def test_a_canceled_subscription_is_not_reactivated():
    svc = _svc()
    _, inv = run(_setup(svc, sub_status="canceled"))
    _settle(svc, inv)
    assert run(svc._subs.fetch("sub_1")).status == "canceled"   # someone who LEFT is not resumed


def test_an_active_subscription_is_untouched():
    svc = _svc()
    _, inv = run(_setup(svc, sub_status="active"))
    _settle(svc, inv)
    assert run(svc._subs.fetch("sub_1")).status == "active"


def test_the_knob_off_disables_auto_reactivate():
    svc = _svc(auto_reactivate=False)
    _, inv = run(_setup(svc, sub_status="paused"))
    _settle(svc, inv)
    assert run(svc._subs.fetch("sub_1")).status == "paused"     # operator turned it off


def test_reactivation_is_idempotent_on_replay():
    svc = _svc()
    _, inv = run(_setup(svc, sub_status="paused"))
    _settle(svc, inv)                                # first settle reactivates
    # a re-settle (idempotent reference) must not error and must leave it active
    run(svc.settle(invoice_id=inv.id, method="bank_transfer", issuer="SABB", reference="MT-1",
                   amount_minor=inv.total_minor, paid_at=time.time()))
    assert run(svc._subs.fetch("sub_1")).status == "active"
