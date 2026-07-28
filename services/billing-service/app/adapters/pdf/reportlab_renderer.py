"""ReportLab invoice PDF — bilingual AR/EN, ZATCA-shaped, typeset rather than printed.

WHAT THIS REPLACED, and why it is worth stating plainly: the previous renderer drew bare strings at
decrementing y-coordinates. It printed amounts in MINOR UNITS, so an invoice for 115.00 SAR read
`11500`. It reshaped Arabic correctly and then drew it in Helvetica, which has no Arabic glyphs at
all, so the Arabic came out as empty boxes. It carried no QR, no dates, no seller or buyer details
and no per-line VAT — every one of which ZATCA requires — and its y-coordinate ran off the bottom
of the page on a long invoice.

FIVE THINGS THIS FILE IS CAREFUL ABOUT

1. **Markup injection.** ReportLab's `Paragraph` parses a mini-XML dialect, so a line description
   containing `<b>` or an unbalanced `<` changes the document or breaks rendering. Every value that
   comes from data is escaped before it reaches a Paragraph. This is the closest thing a PDF has to
   an XSS surface and it is trivially reachable: a line description is user-supplied.

2. **Arabic actually renders, and so does everything else.** TWO faces are in play. The bundled
   Arabic font contains zero Latin letters and zero digits — verified against its cmap — so using
   it as the document font turns every name, number and heading into empty boxes. The first draft
   of this rewrite did exactly that, and it looked correct until somebody rasterised a page and
   looked at it. Latin is a built-in face; Arabic runs switch face inline and switch back.

3. **Money is scaled by the currency's own exponent** — see core/invoice_format. Not every currency
   has two decimal places, and four of the three-decimal ones are Gulf currencies.

4. **The document flows.** Platypus paginates, so a hundred-line invoice produces a hundred-line
   invoice rather than one page and a cliff. Table headers repeat on every page, because a column
   of numbers with no heading on page two is not a document anyone can check.

5. **It never raises into the billing path.** A failed render returns None: an invoice that cannot
   be drawn must not stop the money being recorded. It is logged, loudly, because silently
   returning nothing is how you discover months later that no PDF has been produced since March.
"""
from __future__ import annotations

import io
import logging
import pathlib
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from app.adapters.lazy import load
from app.core.invoice_format import apportion_vat, issued, money, percent

_log = logging.getLogger(__name__)

# Bundled with the service: Droid Sans Arabic, Apache-2.0, © The Android Open Source Project.
# The NOTICE beside it is shipped as the licence requires. Overridable by config so an adopter can
# use a corporate typeface without patching code.
_BUNDLED_FONT = pathlib.Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DroidSansArabic.ttf"
_FONT_NAME = "InvoiceAR"

# A restrained palette. An invoice is a legal document that should look expensive, not decorated:
# one ink, one accent, and a lot of white space.
_INK = (0.11, 0.12, 0.14)
_MUTED = (0.42, 0.45, 0.50)
_HAIRLINE = (0.85, 0.86, 0.88)
_BAND = (0.96, 0.965, 0.97)
# One accent, used only where it earns attention: the masthead band and the amount due. A premium
# document is disciplined, not colourful. Overridable per brand (a hex string like "#0F5F4E").
_ACCENT = (0.06, 0.36, 0.30)
_ACCENT_WASH = (0.93, 0.955, 0.95)


def _hex_rgb(value: str, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    """A brand hex colour (#RRGGBB) as 0–1 RGB, or the fallback if it is absent or malformed."""
    s = str(value or "").strip().lstrip("#")
    if len(s) != 6:
        return fallback
    try:
        return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return fallback


def _shape(text: str) -> str:
    """Shape and reorder Arabic for RTL rendering."""
    if not text:
        return ""
    try:
        reshaper = load("arabic_reshaper", "pdf")
        bidi = load("bidi.algorithm", "pdf")
        return bidi.get_display(reshaper.reshape(text))
    except Exception:  # noqa: BLE001 — shaping is best-effort; unshaped beats nothing
        return text


def _safe(value: Any) -> str:
    """Escape anything data-derived before it reaches a Paragraph.

    `Paragraph` interprets `<b>`, `<font>` and friends. A line description reading
    `<font size=200>` would silently restyle the document, and a bare `<` raises and loses the
    whole invoice. Both are reachable from ordinary user input.
    """
    return _xml_escape("" if value is None else str(value))


def _ar(text: str, font: str | None) -> str:
    """Arabic, escaped, shaped, and switched to the Arabic face for its own run.

    TWO FACES, and this is not a nicety. The bundled Arabic font contains ZERO Latin letters and
    ZERO digits — verified against its cmap — so setting it as the document font renders every
    name, number and heading as empty boxes. That is exactly the defect this renderer replaced,
    only mirrored, and it is invisible unless somebody looks at the page. The base font stays
    Latin; Arabic runs switch face inline and switch back.

    The font tag is written HERE, after the text is escaped, so the markup is ours and the data
    cannot introduce any.
    """
    shaped = _safe(_shape(text))
    if not shaped or not font:
        return shaped
    return f'<font name="{font}">{shaped}</font>' 


class ReportLabPdf:
    driver = "reportlab"

    def __init__(self, *, font_path: str = "", brand: dict[str, Any] | None = None,
                 max_lines: int = 500) -> None:
        self._rl = load("reportlab.platypus", "pdf")
        self._font_path = font_path or str(_BUNDLED_FONT)
        self._brand = brand or {}
        # A cap, because a document is built in memory. An invoice with a hundred thousand lines is
        # a bug upstream, and rendering it would take the service down rather than report it.
        self._max_lines = max_lines
        # Latin/digits come from a built-in face that needs no embedding and covers what the
        # Arabic one cannot. The Arabic face is used only for Arabic runs — see _ar().
        self._font = "Helvetica"
        self._arabic = self._register_font()
        # The accent is a brand choice; a malformed value falls back rather than breaking the page.
        self._accent = _hex_rgb((self._brand.get("accent") or ""), _ACCENT)

    # ── font ──────────────────────────────────────────────────────────────────
    def _register_font(self) -> str | None:
        """Register the Arabic-capable face, or return None so Arabic runs stay unstyled."""
        try:
            pdfmetrics = load("reportlab.pdfbase.pdfmetrics", "pdf")
            ttfonts = load("reportlab.pdfbase.ttfonts", "pdf")
            path = pathlib.Path(self._font_path)
            if not path.is_file():
                # Arabic is mandatory on a KSA tax invoice, so this is loud. The document still
                # renders — the Arabic will be visibly wrong rather than silently dropped, because
                # a missing legal requirement should be obvious on the page, not discovered in an
                # audit.
                _log.error("invoice_arabic_font_missing", extra={"path": str(path)})
                return None
            pdfmetrics.registerFont(ttfonts.TTFont(_FONT_NAME, str(path)))
            return _FONT_NAME
        except Exception as exc:  # noqa: BLE001 — a broken font must not stop invoicing
            _log.error("invoice_arabic_font_registration_failed",
                       extra={"error": type(exc).__name__})
            return None

    def _styles(self):
        ss = load("reportlab.lib.styles", "pdf")
        colors = load("reportlab.lib.colors", "pdf")
        base = ss.getSampleStyleSheet()
        ink = colors.Color(*_INK)
        muted = colors.Color(*_MUTED)
        accent = colors.Color(*self._accent)
        paper = colors.Color(1, 1, 1)

        def style(name, size, leading, color=ink, bold=False, align=0, space_after=0, tracking=0):
            s = ss.ParagraphStyle(
                name, parent=base["Normal"], fontName=self._font, fontSize=size,
                leading=leading, textColor=color, alignment=align, spaceAfter=space_after)
            if tracking:
                s.charSpace = tracking          # letter-spacing on the small-caps labels
            return s

        return {
            "title": style("t", 22, 26, ink, space_after=2),
            "brand": style("br", 15, 18, paper),             # reversed out on the accent band
            "brand_r": style("brr", 9, 13, paper, align=2),  # the Arabic mirror on the band
            "sub": style("s", 9.5, 13, muted),
            "label": style("l", 7.5, 10, muted, tracking=0.6),
            "label_r": style("lr", 7.5, 10, muted, align=2, tracking=0.6),
            "value": style("v", 10, 14, ink),
            "value_r": style("vr", 10, 14, ink, align=2),
            "th": style("th", 8, 11, muted, tracking=0.4),
            "th_r": style("thr", 8, 11, muted, align=2, tracking=0.4),
            "cell": style("c", 9.5, 13, ink),
            "cell_r": style("cr", 9.5, 13, ink, align=2),
            "total": style("tot", 13, 17, ink, align=2),
            "due_label": style("dl", 9, 12, paper),
            "due": style("due", 15, 19, paper, align=2),
            "foot": style("f", 7.5, 10, muted, align=1),
        }

    # ── pieces ────────────────────────────────────────────────────────────────
    def _qr_flowable(self, invoice: dict) -> Any:
        """The ZATCA TLV QR, if the e-invoicing step produced one.

        Absent for an invoice that was never e-invoiced (the null driver, or one still pending), and
        that is a legitimate state — so its absence is not an error, it simply prints nothing.
        """
        tlv = ((invoice.get("einvoice") or {}).get("qr") or "").strip()
        if not tlv:
            return None
        try:
            segno = load("segno", "pdf")
            buf = io.BytesIO()
            segno.make(tlv, error="m").save(buf, kind="png", scale=4, border=0)
            buf.seek(0)
            return self._rl.Image(buf, width=88, height=88)
        except Exception as exc:  # noqa: BLE001
            _log.warning("invoice_qr_render_failed", extra={"error": type(exc).__name__})
            return None

    def _brand_band(self, st, colors, seller: dict, kind_en: str, kind_ar: str):
        """A full-width accent masthead: the seller's name reversed out on the brand colour, with the
        invoice KIND named in both languages — English on the left, Arabic mirrored on the right. It
        is the single strongest 'this is a real, premium document' signal, and it costs one table."""
        name = _safe(seller.get("name", self._brand.get("name", "")))
        band = self._rl.Table(
            [[[self._rl.Paragraph(name, st["brand"]),
               self._rl.Paragraph(_safe(kind_en), st["due_label"])],
              [self._rl.Paragraph(_ar(kind_ar, self._arabic), st["brand_r"])]]],
            colWidths=[300, 199])
        band.setStyle(self._rl.TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.Color(*self._accent)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 18), ("RIGHTPADDING", (-1, 0), (-1, 0), 18),
            ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ]))
        return band

    def _party_block(self, st, title_en: str, title_ar: str, party: dict) -> list:
        rows = [[self._rl.Paragraph(f"{_safe(title_en)} · {_ar(title_ar, self._arabic)}", st["label"])]]
        name = _safe(party.get("name", ""))
        if name:
            rows.append([self._rl.Paragraph(name, st["value"])])
        elif party.get("account"):
            # No named buyer (a simplified B2C invoice): show the tenant as an ACCOUNT reference,
            # clearly labelled — never as a bare legal name, which a raw id is not.
            rows.append([self._rl.Paragraph(
                f"{_safe('Account')} · {_ar('الحساب', self._arabic)}", st["label"])])
            rows.append([self._rl.Paragraph(_safe(party.get("account")), st["sub"])])
        for key, label in (("vat_number", "VAT"), ("cr_number", "CR"), ("address", "")):
            val = _safe(party.get(key, ""))
            if val:
                rows.append([self._rl.Paragraph(f"{label} {val}".strip(), st["sub"])])
        return rows

    def _lines_table(self, st, invoice: dict, colors):
        cur = invoice.get("currency", "")
        lines = list(invoice.get("lines", []) or [])
        truncated = len(lines) > self._max_lines
        if truncated:
            lines = lines[: self._max_lines]
        vat_share = apportion_vat(lines, tax_minor=int(invoice.get("tax_minor", 0) or 0),
                                  taxable_minor=int(invoice.get("subtotal_minor", 0) or 0))

        head = [self._rl.Paragraph(f"DESCRIPTION · {_ar('الوصف', self._arabic)}", st["th"]),
                self._rl.Paragraph("QTY", st["th_r"]),
                self._rl.Paragraph("UNIT", st["th_r"]),
                self._rl.Paragraph(f"VAT · {_ar('الضريبة', self._arabic)}", st["th_r"]),
                self._rl.Paragraph(f"AMOUNT · {_ar('المبلغ', self._arabic)}", st["th_r"])]
        body = [head]
        for line, vat in zip(lines, vat_share):
            body.append([
                self._rl.Paragraph(_safe(line.get("description", "")), st["cell"]),
                self._rl.Paragraph(_safe(line.get("quantity", 1)), st["cell_r"]),
                self._rl.Paragraph(money(line.get("unit_minor", 0), cur, with_currency=False),
                                   st["cell_r"]),
                self._rl.Paragraph(money(vat, cur, with_currency=False), st["cell_r"]),
                self._rl.Paragraph(money(line.get("amount_minor", 0), cur, with_currency=False),
                                   st["cell_r"]),
            ])

        table = self._rl.Table(body, colWidths=[218, 44, 78, 78, 90], repeatRows=1)
        table.setStyle(self._rl.TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), self._font),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            # Hairlines only, and only under rows: a fully boxed grid reads as a spreadsheet.
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, colors.Color(*_INK)),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, colors.Color(*_HAIRLINE)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.Color(*_BAND)]),
        ]))
        return table, truncated

    def _totals(self, st, invoice: dict, colors):
        cur = invoice.get("currency", "")
        rate = percent(invoice.get("vat_rate"))
        rows = [
            ("Subtotal", _ar("المجموع الفرعي", self._arabic), money(invoice.get("subtotal_minor", 0), cur)),
        ]
        discount = int(invoice.get("discount_minor", 0) or 0)
        if discount:
            rows.append(("Discount", _ar("الخصم", self._arabic), "-" + money(discount, cur)))
        rows.append((f"VAT {rate}".strip(), _ar("ضريبة القيمة المضافة", self._arabic),
                     money(invoice.get("tax_minor", 0), cur)))

        data = [[self._rl.Paragraph(f"{_safe(en)} · {ar}", st["sub"]),
                 self._rl.Paragraph(val, st["value_r"])] for en, ar, val in rows]
        # The amount due is the one number that matters, so it gets the accent — a filled panel with
        # the total reversed out, labelled in both languages ("TOTAL DUE / الإجمالي المستحق").
        data.append([self._rl.Paragraph(
                         f"TOTAL DUE · {_ar('الإجمالي المستحق', self._arabic)}", st["due_label"]),
                     self._rl.Paragraph(money(invoice.get("total_minor", 0), cur), st["due"])])

        table = self._rl.Table(data, colWidths=[190, 128], hAlign="RIGHT")
        table.setStyle(self._rl.TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), self._font),
            ("TOPPADDING", (0, 0), (-1, -2), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -2), 5),
            # The grand-total panel.
            ("BACKGROUND", (0, -1), (-1, -1), colors.Color(*self._accent)),
            ("TOPPADDING", (0, -1), (-1, -1), 10),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
            ("LEFTPADDING", (0, -1), (0, -1), 12),
            ("RIGHTPADDING", (-1, -1), (-1, -1), 12),
            ("VALIGN", (0, -1), (-1, -1), "MIDDLE"),
        ]))
        return table

    # ── document ──────────────────────────────────────────────────────────────
    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        try:
            colors = load("reportlab.lib.colors", "pdf")
            st = self._styles()
            buf = io.BytesIO()
            doc = self._rl.SimpleDocTemplate(
                buf, pagesize=(595, 842),
                leftMargin=48, rightMargin=48, topMargin=46, bottomMargin=52,
                title=f"Invoice {invoice.get('number', '')}", author=self._brand.get("name", ""))

            seller = invoice.get("seller") or self._brand.get("seller") or {}
            buyer = invoice.get("buyer") or {}
            simplified = not (buyer.get("vat_number"))
            kind_en = "SIMPLIFIED TAX INVOICE" if simplified else "TAX INVOICE"
            kind_ar = "فاتورة ضريبية مبسطة" if simplified else "فاتورة ضريبية"

            story: list = []
            P = self._rl.Paragraph

            # The masthead is a full-width accent band naming the seller and the invoice KIND in both
            # languages — ZATCA requires the Arabic title, and the band is what makes the document
            # read as premium rather than a printout.
            story += [self._brand_band(st, colors, seller, kind_en, kind_ar),
                      self._rl.Spacer(1, 16)]

            # Beneath it, the invoice's own identity: number and issue date, quietly.
            head = self._rl.Table([[
                [P("INVOICE No. · " + _ar("رقم الفاتورة", self._arabic), st["label"]),
                 P(_safe(invoice.get("number", "")), st["value"])],
                [P("ISSUED · " + _ar("تاريخ الإصدار", self._arabic), st["label_r"]),
                 P(_safe(issued(invoice.get("created", "") or invoice.get("paid_at", ""))),
                   st["value_r"])],
            ]], colWidths=[300, 199])
            head.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story += [head, self._rl.Spacer(1, 18)]

            parties = self._rl.Table(
                [[self._party_block(st, "FROM", "من", seller),
                  self._party_block(st, "BILL TO", "إلى", buyer or {"account": invoice.get("tenant", "")})]],
                colWidths=[250, 249])
            parties.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ]))
            story += [parties, self._rl.Spacer(1, 20)]

            table, truncated = self._lines_table(st, invoice, colors)
            story += [table, self._rl.Spacer(1, 16)]
            if truncated:
                story.append(P(f"Showing the first {self._max_lines} lines of "
                               f"{len(invoice.get('lines', []))}.", st["sub"]))

            qr = self._qr_flowable(invoice)
            footer_left = [qr] if qr else [P("", st["sub"])]
            if qr:
                footer_left.append(P(f"ZATCA · {_ar('رمز الاستجابة السريعة', self._arabic)}", st["label"]))
            summary = self._rl.Table([[footer_left, self._totals(st, invoice, colors)]],
                                     colWidths=[160, 339])
            summary.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (0, 0), "TOP"),
                ("VALIGN", (1, 0), (1, 0), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story += [summary, self._rl.Spacer(1, 26)]

            ein = invoice.get("einvoice") or {}
            trail = " · ".join(x for x in (
                f"UUID {_safe(ein.get('uuid', ''))}" if ein.get("uuid") else "",
                f"ICV {_safe(ein.get('icv', ''))}" if ein.get("icv") else "") if x)
            if trail:
                # The e-invoicing identifiers, discreetly. An auditor reconciling a printed invoice
                # against the ZATCA submission needs them, and hunting through a database for the
                # counterpart of a paper invoice is the thing this one line prevents.
                story.append(P(trail, st["foot"]))

            def _chrome(canvas, document):
                """Drawn on EVERY page, so page two is still identifiably this invoice."""
                canvas.saveState()
                canvas.setFont(self._font, 7.5)
                canvas.setFillColorRGB(*_MUTED)
                left = f"{invoice.get('number', '')} · {seller.get('name', '')}"
                canvas.drawString(48, 34, left[:90])
                canvas.drawRightString(547, 34, f"Page {canvas.getPageNumber()}")
                canvas.setStrokeColorRGB(*_HAIRLINE)
                canvas.setLineWidth(0.4)
                canvas.line(48, 48, 547, 48)
                canvas.restoreState()

            doc.build(story, onFirstPage=_chrome, onLaterPages=_chrome)
            return "application/pdf", buf.getvalue()
        except Exception as exc:  # noqa: BLE001 — a document must never stop the money being recorded
            _log.error("invoice_pdf_render_failed",
                       extra={"invoice": invoice.get("number", ""), "error": type(exc).__name__})
            return None
