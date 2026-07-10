"""Null QR renderer — no image (the base64 TLV is the compliance artifact)."""
from __future__ import annotations


class NullQr:
    driver = "null"

    def render(self, tlv_base64: str) -> tuple[str, bytes] | None:
        return None
