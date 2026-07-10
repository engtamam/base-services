"""SDK-sidecar signer — HTTP client to the official-SDK Java service.

All crypto (XAdES sign, TLV QR, invoice hash, CSR, validate) is done by the SDK
service; this is a thin, secret-free wrapper. Non-2xx / unreachable => fail closed.
"""
from __future__ import annotations

from typing import Any

from app.adapters.zatca_http import post_json
from app.ports.zatca_signer import SignerUnavailable


class SidecarSigner:
    driver = "sdk-sidecar"

    def __init__(self, base_url: str, token: str) -> None:
        if not (base_url and token):
            raise SignerUnavailable("ZATCA_SIGNER_URL and ZATCA_SIGNER_TOKEN are required")
        self._base = base_url.rstrip("/")
        self._headers = {"X-Internal-Token": token}

    def _post(self, path: str, body: dict) -> dict:
        code, resp = post_json(f"{self._base}{path}", body, self._headers)
        if not (200 <= code < 300):
            raise SignerUnavailable(f"zatca signer {path} -> {code}")   # status only, never the body
        return resp

    def generate_csr(self, req: dict[str, Any]) -> dict[str, Any]:
        return self._post("/internal/zatca/csr", req)

    def sign(self, *, invoice_xml: str, certificate_pem: str, private_key_pem: str,
             seller_name: str, vat_number: str, invoice_total: str, vat_total: str,
             simplified: bool) -> dict[str, Any]:
        return self._post("/internal/zatca/sign", {
            "invoiceXml": invoice_xml, "certificatePem": certificate_pem,
            "privateKeyPem": private_key_pem, "sellerName": seller_name, "vatNumber": vat_number,
            "invoiceTotal": invoice_total, "vatTotal": vat_total, "simplified": bool(simplified)})

    def validate(self, signed_xml: str) -> dict[str, Any]:
        return self._post("/internal/zatca/validate", {"signedXml": signed_xml})
