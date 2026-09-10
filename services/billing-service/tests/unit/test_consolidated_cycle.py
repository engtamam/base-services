"""Consolidated / hybrid renewal invoicing modes — one invoice per (tenant, gateway) per sweep.

Money safety is the point: per-subscription mode is unchanged; consolidated cuts ONE invoice with a
line per due subscription, charges once, advances each sub under its OWN idempotency claim (no double
charge/advance), and records one grant per subscription (distinct sources).
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox, source_for
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


class MockGW:
    driver = "mock"

    def charge(self, **k):
        return _Charge(k)

    def verify_webhook(self, **k):
        return None


class _Charge:
    def __init__(self, k):
        self.ok = True
        self.status = "succeeded"
        self.provider = "mock"
        self.provider_id = "ch_" + k.get("idempotency_key", "x")[:8]
        self.amount_minor = k.get("amount_minor", 0)
        self.currency = k.get("currency", "SAR")
        self.error = ""
        self.raw = {}


def _outbox(st):
    from app.core.models import iso, now_ts
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, mode):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": mode, "renewal_gateway_mode": "active"}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=MockGW(), tax=MockTax(), einvoicer=None, pdf=None, grants=_outbox(st), config=cfg)


def _two_due_subs(b, st, tenant="t1", sep2=False):
    p1 = run(b.create_plan(name="AppA", prices=[{"interval": "month", "amount_minor": 10000,
             "currency": "SAR", "type": "flat"}], limits={"credits": 100}))
    p2 = run(b.create_plan(name="AppB", prices=[{"interval": "month", "amount_minor": 5000,
             "currency": "SAR", "type": "flat"}], limits={"credits": 50}))
    s1 = run(b.subscribe(tenant=tenant, plan_id=p1.id, payment_token="pm_1"))
    s2 = run(b.subscribe(tenant=tenant, plan_id=p2.id, payment_token="pm_1"))
    # force both due: period just ended (a realistic renewal, not an epoch-old overdue sub —
    # after one advance the new period_end is a month ahead, so a re-sweep sees them NOT due).
    from app.core.models import now_ts
    ended = now_ts() - 10.0
    for sid in (s1.id, s2.id):
        row = run(InvoiceStore(st)._store.get("subscriptions", sid))
        row["status"] = "active"; row["current_period_end"] = ended
        row["first_period_prorated"] = False        # steady-state renewal: full recurring fee
        if sid == s2.id and sep2:
            row["bill_separately"] = True
        run(InvoiceStore(st)._store.put("subscriptions", sid, row))
    return s1, s2


def _invoices(st):
    return run(st.scan_all("invoices", 100))


def test_consolidated_cuts_one_invoice_with_a_line_per_sub():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    s1, s2 = _two_due_subs(b, st)
    res = run(b.cycle_due_sweep())
    invs = _invoices(st)
    assert res["mode"] == "consolidated" and res["invoiced"] == 1
    assert len(invs) == 1                                    # ONE invoice for the tenant
    inv = invs[0]
    assert len(inv["lines"]) == 2 and inv["subscription_id"] == ""
    assert inv["total_minor"] == 17250                       # (10000+5000) + 15% VAT
    # one charge only
    assert len([p for p in run(st.scan_all("payments", 100))]) == 1
    # both subs advanced past "due"
    for sid in (s1.id, s2.id):
        assert run(SubscriptionStore(st).fetch(sid)).current_period_end > 1.0
    # one grant PER subscription, distinct sources
    assert run(st.get("credit_grants", source_for(inv["id"]) + f":{s1.id}"))
    assert run(st.get("credit_grants", source_for(inv["id"]) + f":{s2.id}"))


def test_per_subscription_mode_still_cuts_two_invoices():
    st = MemoryDataStore(); b = _svc(st, "per_subscription")
    _two_due_subs(b, st)
    run(b.cycle_due_sweep())
    assert len(_invoices(st)) == 2                           # unchanged: one per sub


def test_hybrid_bills_a_flagged_sub_separately():
    st = MemoryDataStore(); b = _svc(st, "hybrid")
    _two_due_subs(b, st, sep2=True)
    run(b.cycle_due_sweep())
    invs = _invoices(st)
    assert len(invs) == 2                                    # 1 consolidated (s1) + 1 separate (s2)
    assert any(len(i["lines"]) == 1 and i["subscription_id"] for i in invs)   # the separate one


def test_consolidated_never_sums_two_currencies_into_one_invoice():
    """Money safety: a SAR sub and a USD sub for the same tenant must NOT be merged — summing across
    currencies is meaningless. They split into two single-currency invoices."""
    from app.core.models import now_ts
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    pu = run(b.create_plan(name="AppU", prices=[{"interval": "month", "amount_minor": 5000,
             "currency": "USD", "type": "flat"}], limits={"credits": 50}))
    ps = run(b.create_plan(name="AppS", prices=[{"interval": "month", "amount_minor": 10000,
             "currency": "SAR", "type": "flat"}], limits={"credits": 100}))
    su = run(b.subscribe(tenant="t1", plan_id=pu.id, payment_token="pm_1"))
    ss = run(b.subscribe(tenant="t1", plan_id=ps.id, payment_token="pm_1"))
    ended = now_ts() - 10.0
    for sid in (su.id, ss.id):
        row = run(InvoiceStore(st)._store.get("subscriptions", sid))
        row["status"] = "active"; row["current_period_end"] = ended
        row["first_period_prorated"] = False
        run(InvoiceStore(st)._store.put("subscriptions", sid, row))
    run(b.cycle_due_sweep())
    invs = _invoices(st)
    assert len(invs) == 2                                     # split by currency, never summed
    assert {i["currency"] for i in invs} == {"USD", "SAR"}
    assert all(len(i["lines"]) == 1 for i in invs)


def test_consolidated_sweep_is_idempotent_no_double_invoice():
    st = MemoryDataStore(); b = _svc(st, "consolidated")
    _two_due_subs(b, st)
    run(b.cycle_due_sweep())
    run(b.cycle_due_sweep())                                  # re-run: subs no longer due + claims held
    assert len(_invoices(st)) == 1
    assert len([p for p in run(st.scan_all("payments", 100))]) == 1
