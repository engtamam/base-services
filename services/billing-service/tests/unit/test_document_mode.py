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


# A COMPLETE tax profile — a real calculator, a rate above zero, a seller registration number.
# A tax invoice is only issued from one of these now; pass a partial via overrides to test refusal.
_SELLER = {"name": "ACME KSA", "vat_number": "300000000000003", "cr_number": "CR-1",
           "address": "Riyadh"}


def _svc(store, *, mode, pdf=None, tax_driver="vat", vat_rate="0.15", seller=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": vat_rate, "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active",),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {}, "period_tolerance_seconds": 86400,
           "grant_fund": "credits", "plan_credit_grant_mode": "accumulate", "document_mode": mode,
           "tax_driver": tax_driver,
           "seller": _SELLER if seller is None else seller}
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


# ── the tax profile must be COMPLETE for a tax invoice to be issued ────────────
def test_tax_mode_with_no_seller_vat_issues_a_receipt_not_a_void_invoice():
    """The core defect: mode flipped to tax_invoice but the seller number is empty. The paid invoice
    must degrade to a RECEIPT — never a document titled 'tax invoice' with no registration number."""
    st = MemoryDataStore()
    svc = _svc(st, mode="tax_invoice", seller={"name": "ACME", "vat_number": ""})
    inv = _paid_invoice(st, svc)
    run(svc._mark_paid(inv))
    assert inv.doc_type == "receipt"              # degraded, not tax
    assert not inv.einvoice                        # no ZATCA finalize on an unbacked profile


def test_tax_mode_with_null_calculator_or_zero_rate_issues_a_receipt():
    for kwargs in ({"tax_driver": "null"}, {"vat_rate": "0"}):
        st = MemoryDataStore()
        svc = _svc(st, mode="tax_invoice", **kwargs)
        inv = _paid_invoice(st, svc)
        run(svc._mark_paid(inv))
        assert inv.doc_type == "receipt" and not inv.einvoice


def test_a_complete_profile_stamps_tax_invoice_and_freezes_the_seller():
    st = MemoryDataStore()
    svc = _svc(st, mode="tax_invoice")
    inv = _paid_invoice(st, svc)
    run(svc._mark_paid(inv))
    assert inv.doc_type == "tax_invoice"
    assert inv.seller["vat_number"] == "300000000000003"   # frozen onto the invoice
    assert inv.einvoice.get("status") == "cleared"


# ── the document follows the INVOICE, not the current setting ──────────────────
def test_pre_switch_receipt_stays_a_receipt_after_switch_to_tax():
    """Acceptance #3: a document issued as a receipt downloads as a receipt even after the tenant
    switches to tax_invoice — the type follows the invoice, not the live mode."""
    st = MemoryDataStore()
    inv = _paid_invoice(st, _svc(st, mode="receipt"))
    run(_svc(st, mode="receipt")._mark_paid(inv))
    assert inv.doc_type == "receipt"
    # now the tenant is fully configured for tax — a NEW service view with the tax mode + full profile
    later = _svc(st, mode="tax_invoice")
    assert later.resolve_document(inv) == "receipt"        # the OLD invoice is untouched


def test_tax_invoice_stays_downloadable_after_switching_back_to_receipt():
    """A tax invoice issued validly stays a tax invoice after the mode flips back to receipt — a
    settings change must never retire a document."""
    st = MemoryDataStore()
    inv = _paid_invoice(st, _svc(st, mode="tax_invoice"))
    run(_svc(st, mode="tax_invoice")._mark_paid(inv))
    assert inv.doc_type == "tax_invoice"
    back = _svc(st, mode="receipt")                        # profile still complete, mode receipt
    assert back.resolve_document(inv) == "tax_invoice"     # still served as the tax invoice it is


def test_render_time_gate_downgrades_when_seller_vat_emptied_after_issue():
    """Acceptance #2: a validly issued tax invoice whose seller number is LATER emptied (a bad
    rollout) falls back to a receipt at render — never an invoice missing its registration."""
    st = MemoryDataStore()
    inv = _paid_invoice(st, _svc(st, mode="tax_invoice"))
    run(_svc(st, mode="tax_invoice")._mark_paid(inv))
    assert inv.doc_type == "tax_invoice"
    broken = _svc(st, mode="tax_invoice", seller={"name": "ACME", "vat_number": ""})
    assert broken.resolve_document(inv) == "receipt"       # the second gate holds
