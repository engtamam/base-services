"""QR renderer: null returns None; segno renders PNG/SVG (optional [qr] extra)."""
from __future__ import annotations

import pytest

from app.adapters.qr.null import NullQr

segno = pytest.importorskip("segno")
from app.adapters.qr.segno_renderer import SegnoQr

TLV = "AQVBQ01FAgQzMDAw"


def test_null_renderer():
    assert NullQr().render(TLV) is None


def test_segno_png():
    ct, data = SegnoQr("png").render(TLV)
    assert ct == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"


def test_segno_svg():
    ct, data = SegnoQr("svg").render(TLV)
    assert ct == "image/svg+xml" and b"<svg" in data


def test_empty_tlv_is_none():
    assert SegnoQr("png").render("") is None
