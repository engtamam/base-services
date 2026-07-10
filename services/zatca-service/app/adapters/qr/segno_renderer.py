"""Segno QR renderer — pure-Python PNG/SVG. Optional [qr] extra, lazy import."""
from __future__ import annotations

import io

from app.adapters.lazy import load


class SegnoQr:
    driver = "segno"

    def __init__(self, fmt: str = "png") -> None:
        self._segno = load("segno", "qr")
        self._fmt = "svg" if fmt == "svg" else "png"

    def render(self, tlv_base64: str) -> tuple[str, bytes] | None:
        if not tlv_base64:
            return None
        try:
            buf = io.BytesIO()
            qr = self._segno.make(tlv_base64, error="m")
            if self._fmt == "svg":
                qr.save(buf, kind="svg", scale=4, border=2)
                return "image/svg+xml", buf.getvalue()
            qr.save(buf, kind="png", scale=4, border=2)
            return "image/png", buf.getvalue()
        except Exception:
            return None
