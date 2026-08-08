"""Null PDF renderer — no PDF (returns None)."""
from __future__ import annotations

from typing import Any


class NullPdf:
    driver = "null"

    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        return None

    def render_receipt(self, invoice: dict[str, Any], payment: dict[str, Any]
                       ) -> tuple[str, bytes] | None:
        return None
