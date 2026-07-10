"""Null e-invoicer — no e-invoicing (dev / non-KSA)."""
from __future__ import annotations

from typing import Any


class NullEInvoicer:
    driver = "null"

    def finalize(self, *, tenant: str, invoice: dict[str, Any]) -> dict[str, Any]:
        return {"status": "skipped"}
