"""ZatcaSigner port — delegate the HARD crypto to the official ZATCA SDK.

We NEVER reimplement XAdES / TLV QR / invoice hash / CSR — the sdk-sidecar adapter
calls the official-SDK service. The null adapter FAILS CLOSED (never fabricates a
signature) so the caller keeps a Phase-1 invoice.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class SignerUnavailable(PortError):
    """Signer not configured/reachable — fail closed (no fabricated signature)."""


@runtime_checkable
class ZatcaSigner(Protocol):
    driver: str

    def generate_csr(self, req: dict[str, Any]) -> dict[str, Any]:
        """{csrPem, privateKeyPem, certificateTemplate, env}."""
        ...

    def sign(self, *, invoice_xml: str, certificate_pem: str, private_key_pem: str,
             seller_name: str, vat_number: str, invoice_total: str, vat_total: str,
             simplified: bool) -> dict[str, Any]:
        """{signedXml, qrBase64, invoiceHash, signatureBase64, signingTimeUtc}."""
        ...

    def validate(self, signed_xml: str) -> dict[str, Any]:
        """{status, detail, exitCode}."""
        ...
