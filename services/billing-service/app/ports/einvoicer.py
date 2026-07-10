"""EInvoicer port — hand a paid invoice to an e-invoicing service (zatca-service).

Decouples billing from ZATCA: the zatca-http adapter POSTs to zatca-service
/internal/zatca/finalize; null does nothing (dev / non-KSA).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EInvoicer(Protocol):
    driver: str

    def finalize(self, *, tenant: str, invoice: dict[str, Any]) -> dict[str, Any]:
        """Submit for e-invoicing. Returns {status, ...}; never raises to the caller."""
        ...
