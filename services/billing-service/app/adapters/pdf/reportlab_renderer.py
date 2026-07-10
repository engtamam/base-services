"""ReportLab PDF renderer — bilingual EN/AR invoice. Optional [pdf] extra, lazy import."""
from __future__ import annotations

import io
from typing import Any

from app.adapters.lazy import load


def _ar(text: str) -> str:
    """Shape + reorder Arabic for correct RTL rendering (best-effort)."""
    try:
        reshaper = load("arabic_reshaper", "pdf")
        bidi = load("bidi.algorithm", "pdf")
        return bidi.get_display(reshaper.reshape(text))
    except Exception:
        return text


class ReportLabPdf:
    driver = "reportlab"

    def __init__(self) -> None:
        self._rl = load("reportlab.pdfgen.canvas", "pdf")

    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        try:
            buf = io.BytesIO()
            c = self._rl.Canvas(buf, pagesize=(595, 842))    # A4 points
            y = 800
            c.setFont("Helvetica-Bold", 16)
            c.drawString(40, y, f"Invoice {invoice.get('number', '')}")
            c.drawRightString(555, y, _ar("فاتورة ضريبية"))
            c.setFont("Helvetica", 10)
            y -= 30
            for label, key in (("Tenant", "tenant"), ("Currency", "currency"),
                               ("Subtotal", "subtotal_minor"), ("Tax (VAT)", "tax_minor"),
                               ("Total", "total_minor"), ("Status", "status")):
                c.drawString(40, y, f"{label}: {invoice.get(key, '')}")
                y -= 18
            y -= 10
            c.setFont("Helvetica-Bold", 11)
            c.drawString(40, y, "Line items")
            c.setFont("Helvetica", 10)
            y -= 18
            for li in invoice.get("lines", []):
                c.drawString(50, y, f"{li.get('description', '')}  x{li.get('quantity', 1)}")
                c.drawRightString(555, y, str(li.get("amount_minor", 0)))
                y -= 16
            c.showPage()
            c.save()
            return "application/pdf", buf.getvalue()
        except Exception:
            return None
