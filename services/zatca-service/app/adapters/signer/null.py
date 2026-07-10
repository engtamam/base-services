"""Null signer — FAILS CLOSED. No signer configured => keep Phase-1, never fabricate."""
from __future__ import annotations

from typing import Any

from app.ports.zatca_signer import SignerUnavailable

_MSG = "ZATCA signer not configured (set ZATCA_SIGNER_DRIVER=sdk-sidecar + URL); invoice stays Phase-1"


class NullSigner:
    driver = "null"

    def generate_csr(self, req: dict[str, Any]) -> dict[str, Any]:
        raise SignerUnavailable(_MSG)

    def sign(self, **kwargs: Any) -> dict[str, Any]:
        raise SignerUnavailable(_MSG)

    def validate(self, signed_xml: str) -> dict[str, Any]:
        raise SignerUnavailable(_MSG)
