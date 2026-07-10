"""QrRenderer port — render the signer's base64 TLV to an image (PNG/SVG).

The TLV itself comes from the signer (the compliance artifact); this is convenience
rendering only. PDF composition is the billing service's job.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class QrRenderer(Protocol):
    driver: str

    def render(self, tlv_base64: str) -> tuple[str, bytes] | None:
        """Return (content_type, image_bytes), or None if rendering is unavailable."""
        ...
