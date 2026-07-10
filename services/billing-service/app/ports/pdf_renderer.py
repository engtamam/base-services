"""PdfRenderer port — render an invoice to a PDF (EN/AR bilingual)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PdfRenderer(Protocol):
    driver: str

    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        """Return (content_type, pdf_bytes), or None if rendering is unavailable."""
        ...
