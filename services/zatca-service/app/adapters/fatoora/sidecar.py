"""Sidecar Fatoora client — submit via the signer sidecar (it holds the PCSID/vault).

Preferred: CSID material never enters this control plane. report/clear POST to the
sidecar's /internal/zatca/{report,clear}. Onboarding stays on the direct adapter.
"""
from __future__ import annotations

from typing import Any

from app.adapters.zatca_http import post_json
from app.ports.fatoora_client import FatooraUnavailable


class SidecarFatoora:
    driver = "sidecar"

    def __init__(self, base_url: str, token: str) -> None:
        if not (base_url and token):
            raise FatooraUnavailable("ZATCA_SIGNER_URL + token required for the sidecar Fatoora client")
        self._base = base_url.rstrip("/")
        self._headers = {"X-Internal-Token": token}

    def _submit(self, path: str, body: dict) -> tuple[int, dict]:
        code, resp = post_json(f"{self._base}{path}", body, self._headers)
        if code == 0:
            raise FatooraUnavailable(f"zatca sidecar {path} unreachable")
        return code, resp

    def report(self, *, signed_xml, invoice_hash, uuid, credentials=None):
        return self._submit("/internal/zatca/report",
                            {"signedXml": signed_xml, "invoiceHash": invoice_hash, "uuid": uuid})

    def clear(self, *, signed_xml, invoice_hash, uuid, credentials=None):
        return self._submit("/internal/zatca/clear",
                            {"signedXml": signed_xml, "invoiceHash": invoice_hash, "uuid": uuid})

    def onboard_compliance(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable("onboarding uses the direct Fatoora client")

    def onboard_production(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable("onboarding uses the direct Fatoora client")
