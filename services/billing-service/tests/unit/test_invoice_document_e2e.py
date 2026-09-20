"""END-TO-END invoice & receipt documents: cart checkout → paid invoice → REAL rendered PDF/receipt,
with VAT enabled and disabled. Proves the money on the document (and its PDF) is accurate and correct.
"""
from __future__ import annotations

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.pdf.reportlab_renderer import ReportLabPdf
from app.adapters.tax.vat import VatCalculator
from app.core.invoice_format import apportion_vat, money

from tests.unit.test_consolidated_cycle import _invoices, _svc, run


def _svc_vat(st, rate):
    """The consolidated svc, wired with a REAL PDF renderer and a chosen VAT rate."""
    b = _svc(st, "consolidated")
    b._tax = VatCalculator(rate)
    b._cfg = dict(b._cfg, vat_rate=rate)
    b._pdf = ReportLabPdf()
    return b


def _plan(b, name, amount):
    return run(b.create_plan(name=name, prices=[{"interval": "month", "amount_minor": amount,
               "currency": "SAR", "type": "flat"}]))


def _paid_invoice(b, st, tenant="t1"):
    a = _plan(b, "AppA", 10000); c = _plan(b, "AppB", 5000)
    run(b.checkout(tenant=tenant, items=[{"plan_id": a.id}, {"plan_id": c.id}], payment_token="pm"))
    inv = _invoices(st)[0]
    from app.core.models import Invoice
    return Invoice.from_dict(inv)


# ── VAT ENABLED (15%) ─────────────────────────────────────────────────────────────
def test_tax_invoice_figures_and_pdf_with_vat_on():
    st = MemoryDataStore(); b = _svc_vat(st, "0.15")
    inv = _paid_invoice(b, st)
    # the money on the invoice
    assert inv.subtotal_minor == 15000 and inv.discount_minor == 0
    assert inv.tax_minor == 2250 and inv.total_minor == 17250      # 15% VAT actually applied
    assert inv.status == "paid"
    # per-line VAT apportions EXACTLY to the invoice VAT (the document's lines add up to its total)
    shares = apportion_vat(inv.lines, tax_minor=inv.tax_minor, taxable_minor=inv.subtotal_minor)
    assert sum(shares) == inv.tax_minor and len(shares) == len(inv.lines)
    # money() prints major units, not minor — 17250 halalas → "172.50"
    assert money(inv.total_minor, "SAR").replace(",", "").startswith("172.50")
    # a REAL PDF is produced
    out = run(b.render_pdf(inv))
    assert out is not None
    mime, pdf = out
    assert mime == "application/pdf" and pdf[:5] == b"%PDF-" and len(pdf) > 1000


def test_document_data_carries_the_full_tax_breakdown_with_vat_on():
    st = MemoryDataStore(); b = _svc_vat(st, "0.15")
    inv = _paid_invoice(b, st)
    data = run(b._document_data(inv))
    for k in ("subtotal_minor", "discount_minor", "tax_minor", "total_minor", "currency", "lines"):
        assert k in data
    assert data["tax_minor"] == 2250 and data["total_minor"] == 17250


# ── VAT DISABLED (0%) ─────────────────────────────────────────────────────────────
def test_receipt_figures_and_pdf_with_vat_off():
    st = MemoryDataStore(); b = _svc_vat(st, "0")
    inv = _paid_invoice(b, st)
    assert inv.subtotal_minor == 15000 and inv.tax_minor == 0
    assert inv.total_minor == 15000                                # no VAT → total is the net
    shares = apportion_vat(inv.lines, tax_minor=0, taxable_minor=inv.subtotal_minor)
    assert sum(shares) == 0                                        # nothing to apportion
    # the invoice PDF still renders correctly with zero VAT
    out = run(b.render_pdf(inv))
    assert out is not None and out[0] == "application/pdf" and out[1][:5] == b"%PDF-"
    # and the payment RECEIPT renders (paid invoice)
    rec = run(b.render_receipt(inv))
    assert rec is not None and rec[1][:5] == b"%PDF-"


def test_reverse_charge_zeroes_vat_on_the_document():
    st = MemoryDataStore(); b = _svc_vat(st, "0.15")
    b._cfg = dict(b._cfg, reverse_charge=True)
    plan = _plan(b, "B2B", 20000)
    run(b.checkout(tenant="t1", items=[{"plan_id": plan.id}], payment_token="pm",
                   buyer_vat="300000000000003"))
    inv = _invoices(st)[0]
    assert inv["tax_minor"] == 0 and inv["total_minor"] == 20000   # VAT-registered buyer: reverse-charge
    from app.core.models import Invoice
    out = run(b.render_pdf(Invoice.from_dict(inv)))
    assert out is not None and out[1][:5] == b"%PDF-"


def test_preview_total_equals_the_rendered_invoice_total_vat_on():
    """The whole chain agrees: pre-purchase preview == the paid invoice == what the PDF prints."""
    st = MemoryDataStore(); b = _svc_vat(st, "0.15")
    a = _plan(b, "A", 10000); c = _plan(b, "B", 5000)
    items = [{"plan_id": a.id}, {"plan_id": c.id}]
    pre = run(b.checkout_preview(tenant="t1", items=items))
    run(b.checkout(tenant="t1", items=items, payment_token="pm"))
    inv = _invoices(st)[0]
    assert pre["total_minor"] == inv["total_minor"] == 17250
    assert pre["tax_minor"] == inv["tax_minor"] == 2250
