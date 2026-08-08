"""PdfRenderer port — render an invoice to a PDF (EN/AR bilingual)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PdfRenderer(Protocol):
    driver: str

    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        """Return (content_type, pdf_bytes) for the TAX invoice, or None if unavailable."""
        ...

    def render_receipt(self, invoice: dict[str, Any], payment: dict[str, Any]
                       ) -> tuple[str, bytes] | None:
        """Return (content_type, pdf_bytes) for the non-tax payment RECEIPT (carrying the gateway
        reference in `payment`), or None if unavailable."""
        ...
