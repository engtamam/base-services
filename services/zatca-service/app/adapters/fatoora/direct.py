"""Direct Fatoora client — call the ZATCA REST gateway directly (needs PCSID creds).

Base URL is env config (host must be egress-allow-listed). Auth = Basic base64(
<binarySecurityToken base64>:<secret>). Body carries base64 of the signed UBL.
"""
from __future__ import annotations

import base64
from typing import Any

from app.adapters.zatca_http import basic_auth, post_json
from app.ports.fatoora_client import FatooraUnavailable


class DirectFatoora:
    driver = "direct"

    def __init__(self, base_url: str) -> None:
        if not base_url:
            raise FatooraUnavailable("FATOORA_BASE_URL is required for the direct client")
        self._base = base_url.rstrip("/")

    def _headers(self, credentials: dict | None, clearance: bool) -> dict[str, str]:
        h = {"Accept-Version": "V2", "Accept-Language": "en", "Accept": "application/json"}
        cred = credentials or {}
        if cred.get("token"):
            h["Authorization"] = basic_auth(cred["token"], cred.get("secret", ""))
        if clearance:
            h["Clearance-Status"] = "1"
        return h

    def _body(self, signed_xml: str, invoice_hash: str, uuid: str) -> dict:
        return {"invoiceHash": invoice_hash, "uuid": uuid,
                "invoice": base64.b64encode(signed_xml.encode()).decode()}

    def report(self, *, signed_xml, invoice_hash, uuid, credentials=None):
        code, resp = post_json(f"{self._base}/invoices/reporting/single",
                               self._body(signed_xml, invoice_hash, uuid),
                               self._headers(credentials, clearance=False))
        if code == 0:
            raise FatooraUnavailable("Fatoora reporting unreachable")
        return code, resp

    def clear(self, *, signed_xml, invoice_hash, uuid, credentials=None):
        code, resp = post_json(f"{self._base}/invoices/clearance/single",
                               self._body(signed_xml, invoice_hash, uuid),
                               self._headers(credentials, clearance=True))
        if code == 0:
            raise FatooraUnavailable("Fatoora clearance unreachable")
        return code, resp

    def onboard_compliance(self, *, csr_pem: str, otp: str) -> tuple[int, dict[str, Any]]:
        code, resp = post_json(f"{self._base}/compliance", {"csr": base64.b64encode(csr_pem.encode()).decode()},
                               {"OTP": otp, "Accept-Version": "V2"})
        if code == 0:
            raise FatooraUnavailable("Fatoora compliance unreachable")
        return code, resp

    def onboard_production(self, *, compliance_request_id: str, csid_token: str,
                           csid_secret: str) -> tuple[int, dict[str, Any]]:
        code, resp = post_json(f"{self._base}/production/csids",
                               {"compliance_request_id": compliance_request_id},
                               {"Accept-Version": "V2", "Authorization": basic_auth(csid_token, csid_secret)})
        if code == 0:
            raise FatooraUnavailable("Fatoora production csid unreachable")
        return code, resp
