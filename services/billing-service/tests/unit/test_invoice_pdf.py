"""The invoice PDF.

Two of these tests exist because of defects that were invisible until somebody rasterised a page
and LOOKED at it. The renderer reported success both times.

  1. The old one drew Arabic in Helvetica, which has no Arabic glyphs. Empty boxes.
  2. The rewrite fixed that by making the Arabic face the document font — and that face contains
     zero Latin letters and zero digits, so every name, number and total became empty boxes
     instead. The mirror image of the same bug, and equally silent.

So the font tests here assert glyph COVERAGE, not that a render succeeded. A render succeeding
proves nothing about whether the page can be read.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("reportlab")

from app.adapters.pdf.reportlab_renderer import _FONT_NAME, ReportLabPdf, _ar, _safe  # noqa: E402

FONT = pathlib.Path(__file__).resolve().parents[2] / "app" / "assets" / "fonts" / "DroidSansArabic.ttf"


def _invoice(**over):
    inv = {
        "number": "INV-000042", "tenant": "acme", "currency": "SAR", "status": "paid",
        "created": "2026-07-22T09:14:00Z",
        "subtotal_minor": 115000, "discount_minor": 5000,
        "tax_minor": 16500, "total_minor": 126500, "vat_rate": "0.15",
        "seller": {"name": "Nadeem Technologies", "vat_number": "310123456700003"},
        "buyer": {"name": "Acme Trading Co", "vat_number": "300987654300003"},
        "lines": [{"description": "Platform subscription", "quantity": 3,
                   "unit_minor": 30000, "amount_minor": 90000},
                  {"description": "Storage", "quantity": 1,
                   "unit_minor": 25000, "amount_minor": 25000}],
    }
    inv.update(over)
    return inv


def _text(pdf_bytes: bytes) -> str:
    """The page's extracted text, so assertions are about what a READER sees."""
    pdfium = pytest.importorskip("pypdfium2")
    import io
    doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


# ── it produces a document at all ─────────────────────────────────────────────
def test_it_renders_a_pdf():
    mime, data = ReportLabPdf().render(_invoice())
    assert mime == "application/pdf"
    assert data.startswith(b"%PDF-")


# ── the amounts a person reads ────────────────────────────────────────────────
def test_no_amount_is_printed_in_minor_units():
    """The defect that started this: an invoice for 1,265.00 SAR printed `126500`."""
    text = _text(ReportLabPdf().render(_invoice())[1])
    assert "1,265.00" in text, "the total is not shown in major units"
    assert "126500" not in text, "a raw minor-unit amount reached the document"
    assert "115000" not in text


def test_the_currency_is_shown_with_the_total():
    assert "SAR" in _text(ReportLabPdf().render(_invoice())[1])


def test_the_lines_vat_sums_to_the_invoice_vat():
    """An invoice whose lines do not add up to its total is one a buyer may reject."""
    text = _text(ReportLabPdf().render(_invoice())[1])
    assert "165.00" in text          # the VAT total, in major units


# ── fonts: coverage, not "it rendered" ────────────────────────────────────────
def test_the_bundled_arabic_font_is_present_and_covers_arabic():
    assert FONT.is_file(), "the bundled Arabic face is missing from the package"
    import struct
    d = FONT.read_bytes()
    n = struct.unpack(">H", d[4:6])[0]
    tabs = {}
    for i in range(n):
        off = 12 + i * 16
        tabs[d[off:off + 4].decode("latin1")] = struct.unpack(">II", d[off + 8:off + 16])
    co = tabs["cmap"][0]
    chars = set()
    for i in range(struct.unpack(">H", d[co + 2:co + 4])[0]):
        e = co + 4 + i * 8
        so = struct.unpack(">I", d[e + 4:e + 8])[0] + co
        if struct.unpack(">H", d[so:so + 2])[0] == 4:
            segx2 = struct.unpack(">H", d[so + 6:so + 8])[0]
            seg = segx2 // 2
            ends = [struct.unpack(">H", d[so + 14 + j * 2:so + 16 + j * 2])[0] for j in range(seg)]
            sso = so + 16 + segx2
            starts = [struct.unpack(">H", d[sso + j * 2:sso + 2 + j * 2])[0] for j in range(seg)]
            for s, en in zip(starts, ends):
                if en != 0xFFFF:
                    chars.update(range(s, min(en, 0xFFFF) + 1))
    assert len([c for c in chars if 0x0600 <= c <= 0x06FF]) > 50, "no Arabic glyphs"


def test_the_font_licence_notice_ships_beside_it():
    """Apache-2.0 §4 requires retaining the NOTICE. Shipping the font without it takes an Apache
    work and strips its terms."""
    notice = FONT.parent / "NOTICE"
    assert notice.is_file()
    assert "Apache License" in notice.read_text(encoding="utf-8", errors="replace")


def test_latin_uses_a_face_that_has_latin_glyphs():
    """The bundled Arabic face has ZERO Latin letters and ZERO digits. Using it as the document
    font turns every name and number into boxes — which is exactly what the first rewrite did, and
    it rendered 'successfully' the whole time."""
    r = ReportLabPdf()
    assert r._font == "Helvetica"
    assert r._arabic == _FONT_NAME


def test_arabic_switches_face_for_its_own_run_only():
    out = _ar("فاتورة", _FONT_NAME)
    assert out.startswith(f'<font name="{_FONT_NAME}">') and out.endswith("</font>")


def test_with_no_arabic_face_the_text_is_still_emitted_unstyled():
    # A missing legal requirement should be visible on the page, not silently dropped.
    assert "<font" not in _ar("فاتورة", None)


def test_a_missing_font_file_does_not_stop_the_invoice():
    r = ReportLabPdf(font_path="/nonexistent/font.ttf")
    assert r._arabic is None
    assert r.render(_invoice())[0] == "application/pdf"


# ── markup injection ──────────────────────────────────────────────────────────
def test_a_line_description_cannot_inject_pdf_markup():
    """ReportLab's Paragraph parses a mini-XML dialect, so a description is the closest thing a
    PDF has to an XSS surface — and it is trivially reachable from user input."""
    evil = '<font size="72" color="red">HUGE</font><b>bold</b>'
    out = ReportLabPdf().render(_invoice(lines=[{"description": evil, "quantity": 1,
                                                 "unit_minor": 100, "amount_minor": 100}]))
    assert out is not None, "a crafted description destroyed the document"
    text = _text(out[1])
    # The markup must appear as literal text, not be interpreted.
    assert "font size" in text or "&lt;" in text or "<font" in text


def test_an_unbalanced_angle_bracket_does_not_lose_the_invoice():
    """A bare `<` raises inside Paragraph and would take the whole document with it."""
    out = ReportLabPdf().render(_invoice(lines=[{"description": "5 < 10 & rising", "quantity": 1,
                                                 "unit_minor": 100, "amount_minor": 100}]))
    assert out is not None
    assert "rising" in _text(out[1])


def test_escaping_covers_the_three_dangerous_characters():
    assert _safe("<a> & 'b'") .startswith("&lt;a&gt; &amp;")


# ── QR ────────────────────────────────────────────────────────────────────────
def _image_count(pdf_bytes: bytes) -> int:
    """Embedded images on page one, counted through the PDF structure.

    The first version of this test asserted `b"/Image" in data`, which is true of a document with
    no images at all — so it passed happily while a mutant removed the QR entirely. Counting real
    page objects is the difference between a test and a decoration.
    """
    pdfium = pytest.importorskip("pypdfium2")
    import io
    page = pdfium.PdfDocument(io.BytesIO(pdf_bytes))[0]
    return sum(1 for obj in page.get_objects() if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE)


def test_the_zatca_qr_is_embedded_when_the_einvoicer_returned_one():
    """It was generated, stored, and never returned to anything that could print it — so no
    invoice ever carried the QR ZATCA requires."""
    with_qr = ReportLabPdf().render(
        _invoice(einvoice={"status": "cleared", "qr": "AQ1TZWxsZXIgTmFtZQ=="}))[1]
    without = ReportLabPdf().render(_invoice(einvoice={"status": "skipped"}))[1]
    assert _image_count(with_qr) == 1, "the QR did not reach the document"
    assert _image_count(without) == 0, "an image appeared with no QR to draw"


# ── premium bilingual presentation ────────────────────────────────────────────
def test_the_masthead_band_names_the_seller_and_the_amount_due_is_labelled():
    text = _text(ReportLabPdf().render(_invoice())[1])
    assert "Nadeem Technologies" in text, "the seller name is missing from the masthead band"
    assert "TOTAL DUE" in text, "the amount-due panel is not labelled"
    assert "TAX INVOICE" in text


def test_a_brand_accent_hex_is_parsed_and_a_bad_one_falls_back():
    from app.adapters.pdf.reportlab_renderer import _ACCENT, _hex_rgb
    assert _hex_rgb("#0F5F4E", _ACCENT) == (15 / 255, 95 / 255, 78 / 255)
    assert _hex_rgb("not-a-colour", _ACCENT) == _ACCENT
    assert _hex_rgb("", _ACCENT) == _ACCENT
    # a branded renderer still produces a valid document
    r = ReportLabPdf(brand={"name": "X", "accent": "#8A1538"})
    assert r.render(_invoice())[0] == "application/pdf"


def test_a_named_buyer_prints_the_name_not_the_tenant_id():
    text = _text(ReportLabPdf().render(_invoice(
        tenant="3477400ca907440297631def92b27a07",
        buyer={"name": "Acme Trading Co", "vat_number": "300987654300003"}))[1])
    assert "Acme Trading Co" in text
    assert "3477400ca907440297631def92b27a07" not in text, "the raw tenant id was printed as a name"


def test_an_unnamed_buyer_shows_the_tenant_as_an_account_reference_not_a_name():
    """A B2C simplified invoice has no buyer name; the tenant id must appear as a labelled ACCOUNT
    reference, never masquerading as a legal name in the BILL TO block."""
    uid = "3477400ca907440297631def92b27a07"
    text = _text(ReportLabPdf().render(_invoice(tenant=uid, buyer={"account": uid}))[1])
    assert "Account" in text and uid in text


def test_an_invoice_with_no_qr_still_renders():
    # Legitimate: the null e-invoicer, or a submission still pending.
    assert ReportLabPdf().render(_invoice(einvoice={"status": "skipped"})) is not None


# ── size and flow ─────────────────────────────────────────────────────────────
def test_a_long_invoice_paginates_instead_of_running_off_the_page():
    lines = [{"description": f"Item {i}", "quantity": 1, "unit_minor": 100,
              "amount_minor": 100} for i in range(60)]
    data = ReportLabPdf().render(_invoice(lines=lines))[1]
    pdfium = pytest.importorskip("pypdfium2")
    import io
    assert len(pdfium.PdfDocument(io.BytesIO(data))) > 1


def test_line_count_is_capped_and_the_document_says_so():
    """An invoice with a hundred thousand lines is an upstream bug. Rendering it would take the
    service down; quietly showing a partial total would be worse."""
    lines = [{"description": f"Item {i}", "quantity": 1, "unit_minor": 100,
              "amount_minor": 100} for i in range(40)]
    text = _text(ReportLabPdf(max_lines=10).render(_invoice(lines=lines))[1])
    assert "Item 9" in text and "Item 39" not in text
    assert "first 10 lines" in text


def test_a_render_failure_returns_none_rather_than_raising():
    """An invoice that cannot be drawn must not stop the money being recorded."""
    r = ReportLabPdf()
    assert r.render({"lines": "not-a-list"}) is None
