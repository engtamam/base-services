"""FatooraClient port — submit to ZATCA Fatoora (report B2C / clear B2B) + onboard.

report/clear return a normalized outcome (never raise on a ZATCA rejection — that is
a real answer to classify). Only a genuinely unreachable gateway raises
FatooraUnavailable so the invoice stays 'pending' and the retry sweep tries later.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class FatooraUnavailable(PortError):
    """Gateway/sidecar unreachable or unconfigured — retryable (invoice stays pending)."""


@runtime_checkable
class FatooraClient(Protocol):
    driver: str

    def report(self, *, signed_xml: str, invoice_hash: str, uuid: str,
               credentials: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """POST reporting/single (simplified/B2C). Returns (http_code, body). `credentials`
        (CSID token/secret) is used by the direct adapter; the sidecar holds its own."""
        ...

    def clear(self, *, signed_xml: str, invoice_hash: str, uuid: str,
              credentials: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """POST clearance/single (standard/B2B). Returns (http_code, body)."""
        ...

    def onboard_compliance(self, *, csr_pem: str, otp: str) -> tuple[int, dict[str, Any]]:
        ...

    def onboard_production(self, *, compliance_request_id: str, csid_token: str,
                           csid_secret: str) -> tuple[int, dict[str, Any]]:
        ...
