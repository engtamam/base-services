"""ZATCA e-invoicer — POST the paid invoice to zatca-service /internal/zatca/finalize.

Never raises to the caller: an unreachable zatca-service returns {status: deferred}
so billing doesn't fail the payment (billing already collected the money).
"""
from __future__ import annotations

from typing import Any

from app.adapters.billing_http import post_json
from app.core.internal_client import assertion_headers_from_env


class ZatcaHttpEInvoicer:
    driver = "zatca-http"

    def __init__(self, base_url: str, token: str) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token

    def finalize(self, *, tenant: str, invoice: dict[str, Any]) -> dict[str, Any]:
        if not (self._base and self._token):
            return {"status": "deferred", "reason": "zatca-service not configured"}
        code, resp = post_json(f"{self._base}/internal/zatca/finalize",
                               {"tenant": tenant, "invoice": invoice},
                               {"X-Internal-Token": self._token,
                                **assertion_headers_from_env(target="zatca-service")})
        if not (200 <= code < 300):
            return {"status": "deferred", "code": code}
        return {"status": resp.get("status", "pending"), "invoice_id": resp.get("invoice_id"),
                "icv": resp.get("icv"),
                # Carried through so the invoice PDF can print it. Dropping it here was why the
                # QR existed and no document ever showed one.
                "qr": resp.get("qr", ""), "uuid": resp.get("uuid", "")}
