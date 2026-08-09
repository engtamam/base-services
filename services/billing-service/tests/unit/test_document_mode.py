"""BILLING_DOCUMENT_MODE — receipt (default) vs tax_invoice, and the receipt's gateway reference (56)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.models import Invoice, Payment
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


class _Einvoicer:
    def finalize(self, *, tenant, invoice):
        return {"uuid": "u-1", "icv": "1", "status": "cleared"}   # a truthy, "finalized" e-invoice


class _CapturePdf:
    driver = "capture"

    def __init__(self):
        self.receipt_payment = None

    def render(self, invoice):
        return ("application/pdf", b"%PDF-tax")

    def render_receipt(self, invoice, payment):
        self.receipt_payment = payment
        return ("application/pdf", b"%PDF-receipt")


def _svc(store, *, mode, pdf=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}, "period_tolerance_seconds": 86400,
           "grant_fund": "credits", "plan_credit_grant_mode": "accumulate", "document_mode": mode}
    return BillingService(
        plans=PlanStore(store), coupons=CouponStore(store), subs=SubscriptionStore(store),
        invoices=InvoiceStore(store), payments=PaymentStore(store), usage=UsageStore(store),
        ledger=LedgerStore(store), idempo=IdempotencyStore(store), gateway=None, tax=None,
        einvoicer=_Einvoicer(), pdf=pdf, settlements=SettlementStore(store), config=cfg)


def _paid_invoice(store, svc):
    inv = Invoice(id="inv_1", tenant="acme", number="INV-1", status="open", total_minor=115000,
                  currency="SAR")
    run(InvoiceStore(store).save(inv))
    return inv


# ── the mode gates the ZATCA e-invoice ────────────────────────────────────────
def test_receipt_mode_does_not_finalize_the_einvoice():
    st = MemoryDataStore()
    svc = _svc(st, mode="receipt")
    inv = _paid_invoice(st, svc)
    run(svc._mark_paid(inv))
    assert inv.status == "paid"
    assert not inv.einvoice                       # receipt-only: no ZATCA finalize


def test_tax_invoice_mode_finalizes_the_einvoice():
    st = MemoryDataStore()
    svc = _svc(st, mode="tax_invoice")
    inv = _paid_invoice(st, svc)
    run(svc._mark_paid(inv))
    assert inv.einvoice.get("status") == "cleared"


def test_both_mode_finalizes_the_einvoice():
    st = MemoryDataStore()
    svc = _svc(st, mode="both")
    inv = _paid_invoice(st, svc)
    run(svc._mark_paid(inv))
    assert inv.einvoice.get("uuid") == "u-1"


# ── the receipt carries the gateway reference from the stored Payment ──────────
def test_receipt_pulls_the_gateway_reference_from_the_payment():
    st = MemoryDataStore()
    pdf = _CapturePdf()
    svc = _svc(st, mode="receipt", pdf=pdf)
    inv = Invoice(id="inv_1", tenant="acme", number="INV-1", status="paid", total_minor=115000,
                  currency="SAR", paid_at="2026-07-22T09:15:00Z")
    run(InvoiceStore(st).save(inv))
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="acme", invoice_id="inv_1",
                                      provider="moyasar", provider_id="mpay_9", status="succeeded",
                                      currency="SAR")))
    out = run(svc.render_receipt(inv))
    assert out is not None
    assert pdf.receipt_payment["provider"] == "moyasar"
    assert pdf.receipt_payment["provider_id"] == "mpay_9"


def test_a_settlement_paid_invoice_receipt_uses_the_settlement_reference():
    st = MemoryDataStore()
    pdf = _CapturePdf()
    svc = _svc(st, mode="receipt", pdf=pdf)
    inv = Invoice(id="inv_2", tenant="acme", number="INV-2", status="paid", total_minor=100,
                  currency="SAR")
    run(InvoiceStore(st).save(inv))
    from app.core.models import Settlement
    run(SettlementStore(st).save(Settlement(id="stl_1", tenant="acme", invoice_id="inv_2",
                                            method="bank_transfer", reference="REF-9001",
                                            amount_minor=100, currency="SAR")))
    run(svc.render_receipt(inv))
    assert pdf.receipt_payment["method"] == "bank_transfer"
    assert pdf.receipt_payment["reference"] == "REF-9001"


def test_receipt_for_an_unpaid_invoice_is_none():
    st = MemoryDataStore()
    svc = _svc(st, mode="receipt", pdf=_CapturePdf())
    inv = Invoice(id="inv_3", tenant="acme", number="INV-3", status="open", total_minor=100,
                  currency="SAR")
    run(InvoiceStore(st).save(inv))
    assert run(svc.render_receipt(inv)) is None
