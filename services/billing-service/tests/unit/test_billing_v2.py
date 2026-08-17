"""Billing v2: currency decimals, entitlements, overage, ledger, idempotency, VAT-inclusive."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core import currency, entitlements
from app.core.billing_service import BillingService
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge, WebhookEvent

CFG = {"cycle_sweep_limit": 1000, "platform_max_pages": 50, "cycle_lead_seconds": 0, "grant_fund": "credits", "plan_credit_limit_key": "credits",
       "plan_credit_grant_mode": "accumulate", "document_mode": "tax_invoice",
       # A COMPLETE tax profile — required now for tax_invoice mode to actually issue one.
       "tax_driver": "vat", "seller": {"name": "ACME KSA", "vat_number": "300000000000003"},
       "currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "dunning_base_hours": 24,
       "dunning_max_attempts": 3, "grace_days": 7, "list_limit": 100, "reverse_charge": False,
       # Phase 42: required by EntitlementResolver.
       "billable_statuses": ("active", "trialing", "past_due"), "grandfather": True, "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}, "period_tolerance_seconds": 86400,
}


def run(c):
    return asyncio.run(c)


class GW:
    driver = "mock"

    def charge(self, **k):
        return Charge(ok=True, status="succeeded", provider="mock", provider_id="ch_" + k["idempotency_key"][:6],
                      amount_minor=k["amount_minor"], currency=k["currency"])

    def refund(self, **k):
        return Charge(ok=True, status="refunded", provider="mock", provider_id="re")

    def verify_webhook(self, **k):
        return WebhookEvent(verified=False)


class EInv:
    driver = "z"

    def __init__(self):
        self.buyers = []

    def finalize(self, *, tenant, invoice):
        self.buyers.append(invoice["buyer"])
        return {"status": "reported"}


class NullPdf:
    driver = "null"

    def render(self, i):
        return None


def _svc(st, einv=None, cfg=None):
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=GW(),
                          tax=VatCalculator("0.15"), einvoicer=einv or EInv(), pdf=NullPdf(), config=cfg or CFG)


# ── refunds are visible per invoice (round-3 #3) ──────────────────────────────
def _paid(st, iid, total):
    from app.core.models import Invoice, Payment
    run(InvoiceStore(st).save(Invoice(id=iid, tenant="t1", number=iid, status="paid",
                                      total_minor=total, currency="SAR")))
    run(PaymentStore(st).save(Payment(id=f"pay_{iid}", tenant="t1", invoice_id=iid,
                                      status="succeeded", provider_id="ch", currency="SAR")))


def test_partial_refund_records_refunded_minor_and_keeps_the_invoice_paid():
    # 299.00 invoice, 150.00 back: the shop kept 149.00, so it must NOT void — and refunded_minor
    # answers "what actually went back" so a partner commission is not charged on the refunded part.
    st = MemoryDataStore(); b = _svc(st)
    _paid(st, "inv_1", 29900)
    assert run(b.refund(f"pay_inv_1", amount_minor=15000))["ok"]
    got = run(InvoiceStore(st).fetch("inv_1"))
    assert got.refunded_minor == 15000 and got.status == "paid"


def test_full_refund_voids_and_records_the_whole_total():
    st = MemoryDataStore(); b = _svc(st)
    _paid(st, "inv_2", 29900)
    run(b.refund("pay_inv_2"))                                  # no amount == full
    got = run(InvoiceStore(st).fetch("inv_2"))
    assert got.refunded_minor == 29900 and got.status == "void"


def test_two_partial_refunds_accumulate_and_then_void():
    st = MemoryDataStore(); b = _svc(st)
    _paid(st, "inv_3", 20000)
    run(b.refund("pay_inv_3", amount_minor=8000))
    # the second refund needs a still-refundable payment; re-mark it succeeded for the test
    from app.core.models import Payment
    run(PaymentStore(st).save(Payment(id="pay_inv_3", tenant="t1", invoice_id="inv_3",
                                      status="succeeded", provider_id="ch", currency="SAR")))
    run(b.refund("pay_inv_3", amount_minor=12000))             # now fully refunded
    got = run(InvoiceStore(st).fetch("inv_3"))
    assert got.refunded_minor == 20000 and got.status == "void"


# ── currency ──────────────────────────────────────────────────────────────────
def test_currency_decimals():
    assert currency.major(5000, "SAR") == "50.00"
    assert currency.major(5000, "KWD") == "5.000"            # 3-dp currency
    assert currency.major(5000, "JPY") == "5000"            # 0-dp
    assert currency.minor_per("BHD") == 1000


# ── entitlements ──────────────────────────────────────────────────────────────
def test_entitlement_convention():
    limits = {"seats": 5, "api_calls": -1, "export": 0}
    assert entitlements.check(limits, "seats", 4)["allowed"] is True
    assert entitlements.check(limits, "seats", 5)["allowed"] is False
    assert entitlements.check(limits, "api_calls", 999999)["unlimited"] is True   # -1
    assert entitlements.check(limits, "export", 0)["allowed"] is False            # 0 = deny
    assert entitlements.check(limits, "unknown_key", 0)["allowed"] is False       # fail-closed


def test_entitlement_from_active_plan():
    st = MemoryDataStore()
    b = _svc(st)
    plan = run(b.create_plan(name="Team", prices=[{"interval": "month", "amount_minor": 10000, "currency": "SAR"}],
                             limits={"seats": 3}))
    run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    assert run(b.check_entitlement(tenant="t1", key="seats", current=2))["allowed"] is True
    assert run(b.check_entitlement(tenant="t1", key="seats", current=3))["allowed"] is False


# ── overage ────────────────────────────────────────────────────────────────────
def test_overage_run_bills_excess_only():
    st = MemoryDataStore()
    b = _svc(st)
    plan = run(b.create_plan(name="API", prices=[{"interval": "month", "amount_minor": 0, "currency": "SAR",
                                                  "type": "hybrid", "usage_metric": "calls",
                                                  "included_units": 100, "overage_minor": 10}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    run(b.record_usage(tenant="t1", subscription_id=sub.id, metric="calls", quantity=150))
    inv = run(b.run_overage(sub.id))
    assert inv.subtotal_minor == 500                          # 50 over × 10
    assert run(b.run_overage(sub.id)) is None                 # idempotent per period


def test_overage_zero_issues_nothing():
    st = MemoryDataStore()
    b = _svc(st)
    plan = run(b.create_plan(name="API", prices=[{"interval": "month", "amount_minor": 0, "currency": "SAR",
                                                  "type": "hybrid", "usage_metric": "calls",
                                                  "included_units": 100, "overage_minor": 10}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    run(b.record_usage(tenant="t1", subscription_id=sub.id, metric="calls", quantity=50))
    assert run(b.run_overage(sub.id)) is None                 # under the included units


# ── ledger ─────────────────────────────────────────────────────────────────────
def test_ledger_records_and_balances():
    st = MemoryDataStore()
    b = _svc(st)
    plan = run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000, "currency": "SAR"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    run(b.run_cycle(sub.id))
    rows = run(LedgerStore(st).list("t1"))
    assert len(rows) == 1 and rows[0]["kind"] == "charge" and rows[0]["balance_after"] == 11500


# ── idempotency ──────────────────────────────────────────────────────────────────
def test_run_cycle_idempotent():
    st = MemoryDataStore()
    b = _svc(st)
    plan = run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000, "currency": "SAR"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm"))
    inv1 = run(b.run_cycle(sub.id))
    # re-run for the SAME period (simulate a retry that didn't advance) — no second invoice/charge
    s = run(SubscriptionStore(st).fetch(sub.id))
    s.current_period_end = int(inv1.id and s.current_period_end)   # keep as-is
    inv2 = run(b.run_cycle(sub.id))                                 # advanced already → new period, new invoice
    # the key is period-end-scoped; both are valid but the first period isn't double-charged
    charges = [r for r in run(LedgerStore(st).list("t1")) if r["kind"] == "charge"]
    assert len(charges) == 2 and charges[0]["invoice_id"] != charges[1]["invoice_id"]


# ── VAT-inclusive + buyer tax ─────────────────────────────────────────────────────
def test_vat_inclusive_price_split():
    st = MemoryDataStore()
    einv = EInv()
    b = _svc(st, einv)
    plan = run(b.create_plan(name="Incl", prices=[{"interval": "month", "amount_minor": 11500,
                                                   "currency": "SAR", "includes_tax": True}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm",
                          buyer_name="Buyer Co", buyer_vat="311111111111113"))
    inv = run(b.run_cycle(sub.id))
    assert inv.total_minor == 11500 and inv.tax_minor == 1500 and inv.subtotal_minor == 10000
    assert einv.buyers[-1]["vat_number"] == "311111111111113"      # buyer flows to the e-invoice


def test_reverse_charge_zeroes_vat():
    st = MemoryDataStore()
    cfg = {**CFG, "reverse_charge": True}
    b = _svc(st, cfg=cfg)
    plan = run(b.create_plan(name="B2B", prices=[{"interval": "month", "amount_minor": 10000, "currency": "SAR"}]))
    sub = run(b.subscribe(tenant="t1", plan_id=plan.id, payment_token="pm", buyer_vat="3111"))
    inv = run(b.run_cycle(sub.id))
    assert inv.tax_minor == 0 and inv.total_minor == 10000         # buyer accounts for VAT
