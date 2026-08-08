"""The internal payment RECEIPT (non-tax) — 56.2.

A receipt confirms money received and carries the gateway's transaction reference. It is NOT a tax
document: no VAT lines, no ZATCA QR. These tests render a real PDF and read its text back.
"""
from __future__ import annotations

import pytest

pytest.importorskip("reportlab")

from app.adapters.pdf.reportlab_renderer import ReportLabPdf  # noqa: E402


def _invoice(**over):
    inv = {"number": "INV-000042", "tenant": "acme", "currency": "SAR", "status": "paid",
           "created": "2026-07-22T09:14:00Z", "paid_at": "2026-07-22T09:15:00Z",
           "subtotal_minor": 115000, "tax_minor": 0, "total_minor": 115000, "vat_rate": "0",
           "seller": {"name": "Nadeem Technologies"},
           "buyer": {"account": "acme"},
           "lines": [{"description": "Platform subscription", "quantity": 1,
                      "unit_minor": 115000, "amount_minor": 115000}]}
    inv.update(over)
    return inv


def _text(pdf: bytes) -> str:
    pdfium = pytest.importorskip("pypdfium2")
    import io
    doc = pdfium.PdfDocument(io.BytesIO(pdf))
    return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


def test_receipt_renders_a_pdf():
    out = ReportLabPdf().render_receipt(_invoice(), {"provider": "moyasar", "provider_id": "pay_123"})
    assert out is not None and out[0] == "application/pdf" and out[1].startswith(b"%PDF-")


def test_receipt_carries_the_gateway_reference():
    text = _text(ReportLabPdf().render_receipt(
        _invoice(), {"provider": "moyasar", "provider_id": "pay_123"})[1])
    assert "moyasar" in text and "pay_123" in text          # the gateway transaction reference
    assert "1,150.00" in text                                # amount paid, major units
    assert "PAYMENT RECEIPT" in text


def test_receipt_says_it_is_not_a_tax_invoice_and_has_no_vat_line():
    text = _text(ReportLabPdf().render_receipt(
        _invoice(), {"provider": "stripe", "provider_id": "ch_1"})[1])
    assert "not a tax invoice" in text
    assert "VAT" not in text                                  # no tax line at all
    assert "TOTAL DUE" not in text                            # a receipt shows AMOUNT PAID, not due


def test_a_settlement_receipt_shows_method_and_reference():
    text = _text(ReportLabPdf().render_receipt(
        _invoice(), {"method": "bank_transfer", "reference": "REF-9001"})[1])
    assert "bank_transfer" in text and "REF-9001" in text     # off-gateway: method + bank reference
