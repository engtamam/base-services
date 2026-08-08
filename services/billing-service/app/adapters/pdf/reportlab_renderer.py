"""ReportLab invoice/receipt PDF — Arabic, ZATCA-shaped, typeset rather than printed.

WHAT THIS REPLACED, and why it is worth stating plainly: the previous renderer drew bare strings at
decrementing y-coordinates. It printed amounts in MINOR UNITS, so an invoice for 115.00 SAR read
`11500`. It reshaped Arabic correctly and then drew it in Helvetica, which has no Arabic glyphs at
all, so the Arabic came out as empty boxes. It carried no QR, no dates, no seller or buyer details
and no per-line VAT — every one of which ZATCA requires — and its y-coordinate ran off the bottom
of the page on a long invoice.

ARABIC-FIRST. The KSA market reads Arabic; a bilingual document doubles every label and dilutes the
look. So the visible chrome is Arabic ONLY — «فاتورة ضريبية», «سند قبض», «الإجمالي المستحق» — and the
layout reads right-to-left. The single hard rule this creates is the one below.

FIVE THINGS THIS FILE IS CAREFUL ABOUT

1. **Markup injection.** ReportLab's `Paragraph` parses a mini-XML dialect, so a line description
   containing `<b>` or an unbalanced `<` changes the document or breaks rendering. Every value that
   comes from data is escaped before it reaches a Paragraph. This is the closest thing a PDF has to
   an XSS surface and it is trivially reachable: a line description is user-supplied.

2. **Arabic renders, and so do the digits.** TWO faces are in play, and this is not a nicety. The
   bundled Arabic font contains ZERO Latin letters and ZERO digits — verified against its cmap — so
   setting it as the document font turns every NUMBER, currency code and time into empty boxes. An
   Arabic-only document is therefore NOT an Arabic-font document: Arabic runs switch to the Arabic
   face and back, while amounts, dates and references stay on the Latin face. `_auto()` splits mixed
   text into runs so a seller name like «شركة نديم 2026» renders both halves correctly.

3. **Money is scaled by the currency's own exponent** — see core/invoice_format. Not every currency
   has two decimal places, and four of the three-decimal ones are Gulf currencies.

4. **The document flows.** Platypus paginates, so a hundred-line invoice produces a hundred-line
   invoice rather than one page and a cliff. Table headers repeat on every page.

5. **It never raises into the billing path.** A failed render returns None: an invoice that cannot
   be drawn must not stop the money being recorded. It is logged, loudly.
"""
from __future__ import annotations

import io
import logging
import pathlib
import re
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from app.adapters.lazy import load
from app.core.invoice_format import apportion_vat, money, percent

_log = logging.getLogger(__name__)

# Bundled with the service: Droid Sans Arabic, Apache-2.0, © The Android Open Source Project.
# The NOTICE beside it is shipped as the licence requires. Overridable by config so an adopter can
# use a corporate typeface without patching code.
_BUNDLED_FONT = pathlib.Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DroidSansArabic.ttf"
_FONT_NAME = "InvoiceAR"

# Any codepoint in an Arabic block — used to find the runs that must switch to the Arabic face.
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

# Gregorian month names in Arabic (the calendar KSA invoices use), so a date reads «22 يوليو 2026»
# rather than «22 July 2026». Digits stay Latin — see the two-faces rule above.
_AR_MONTHS = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
              "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")

# A restrained palette. An invoice is a legal document that should look expensive, not decorated:
# one ink, one accent, and a lot of white space.
_INK = (0.11, 0.12, 0.14)
_MUTED = (0.42, 0.45, 0.50)
_HAIRLINE = (0.85, 0.86, 0.88)
_BAND = (0.96, 0.965, 0.97)
# One accent, used only where it earns attention: the masthead band and the amount due. A premium
# document is disciplined, not colourful. Overridable per brand (a hex string like "#0F5F4E").
_ACCENT = (0.06, 0.36, 0.30)


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
    """A KNOWN-Arabic label, escaped, shaped, and switched to the Arabic face for its own run.

    TWO FACES, and this is not a nicety. The bundled Arabic font contains ZERO Latin letters and
    ZERO digits, so setting it as the document font renders every name, number and heading as empty
    boxes — the mirror of the defect this renderer replaced. The base font stays Latin; Arabic runs
    switch face inline and switch back. The font tag is written HERE, after the text is escaped, so
    the markup is ours and the data cannot introduce any."""
    shaped = _safe(_shape(text))
    if not shaped or not font:
        return shaped
    return f'<font name="{font}">{shaped}</font>'


def _split_runs(text: str) -> list[tuple[str, bool]]:
    """Break text into contiguous (run, is_arabic) segments, so each renders in the right face."""
    runs: list[tuple[str, bool]] = []
    for ch in text:
        is_ar = bool(_ARABIC_RE.match(ch))
        if runs and runs[-1][1] == is_ar:
            runs[-1] = (runs[-1][0] + ch, is_ar)
        else:
            runs.append((ch, is_ar))
    return runs


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
        # Arabic one cannot. The Arabic face is used only for Arabic runs — see _auto()/_ar().
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
                # a missing legal requirement should be obvious on the page, not found in an audit.
                _log.error("invoice_arabic_font_missing", extra={"path": str(path)})
                return None
            pdfmetrics.registerFont(ttfonts.TTFont(_FONT_NAME, str(path)))
            return _FONT_NAME
        except Exception as exc:  # noqa: BLE001 — a broken font must not stop invoicing
            _log.error("invoice_arabic_font_registration_failed",
                       extra={"error": type(exc).__name__})
            return None

    # ── text: the two-faces rule made reusable ──────────────────────────────────
    def _ar(self, text: str) -> str:
        """A KNOWN-Arabic label on this renderer's Arabic face — see the module-level _ar()."""
        return _ar(text, self._arabic)

    def _auto(self, value: Any) -> str:
        """DATA of unknown script: Arabic runs shaped and switched to the Arabic face, Latin and
        digits left on the base face. This is what lets a seller name, a buyer name or a line
        description be Arabic, Latin, or a mix — without boxes on either side."""
        s = "" if value is None else str(value)
        if not s:
            return ""
        if not self._arabic or not _ARABIC_RE.search(s):
            return _safe(s)
        out = []
        for run, is_ar in _split_runs(s):
            out.append(f'<font name="{self._arabic}">{_safe(_shape(run))}</font>'
                       if is_ar else _safe(run))
        return "".join(out)

    def _issued(self, value: str) -> str:
        """An ISO timestamp as Arabic reads it: «٢٢ يوليو ٢٠٢٦ — ٠٩:١٤», rendered as ONE Arabic run so
        bidi orders day/month/year/time coherently (a Latin-digit mix scrambled them). Arabic-Indic
        digits keep it a single run on a face that has those glyphs. Unparseable input is returned."""
        raw = (value or "").strip()
        if not raw:
            return ""
        try:
            date_part, _, time_part = raw.partition("T")
            y, m, d = (int(x) for x in date_part.split("-"))
            if not 1 <= m <= 12:
                return _safe(raw)
            out = f"{d} {self._ar(_AR_MONTHS[m - 1])} {y}"
            hhmm = time_part[:5]
            if len(hhmm) == 5 and hhmm[2] == ":":
                out += f" · {hhmm}"
            return out
        except (ValueError, IndexError):
            return _safe(raw)

    def _styles(self):
        ss = load("reportlab.lib.styles", "pdf")
        colors = load("reportlab.lib.colors", "pdf")
        base = ss.getSampleStyleSheet()
        ink = colors.Color(*_INK)
        muted = colors.Color(*_MUTED)
        paper = colors.Color(1, 1, 1)

        def style(name, size, leading, color=ink, align=2, space_after=0, tracking=0, rtl=True):
            s = ss.ParagraphStyle(
                name, parent=base["Normal"], fontName=self._font, fontSize=size,
                leading=leading, textColor=color, alignment=align, spaceAfter=space_after)
            if rtl:
                # RTL base direction, so a pure-Arabic label/heading orders correctly. Lines that MIX
                # an Arabic label with a Latin number or date use an LTR variant instead — under RTL
                # the number and its separator jump to the wrong edge.
                s.wordWrap = "RTL"
            if tracking:
                s.charSpace = tracking
            return s

        # align: 0 left, 1 centre, 2 right. An Arabic document reads right-to-left, so right is the
        # default and the exceptions are deliberate.
        return {
            "brand": style("br", 17, 22, paper, align=2),        # seller name, reversed on the band
            "brand_kind": style("brk", 9, 13, paper, align=2, tracking=1.0),  # «فاتورة ضريبية»
            "sub": style("s", 9.5, 14, muted, align=2),
            "sub_x": style("sx", 9.5, 14, muted, align=2, rtl=False),   # right-aligned, mixed AR+digits
            "label": style("l", 8, 11, muted, align=2, tracking=0.4),
            "value": style("v", 10.5, 15, ink, align=2),
            "value_x": style("vx", 10.5, 15, ink, align=2, rtl=False),  # right-aligned, mixed AR+digits
            "th": style("th", 8.5, 12, muted, align=2, tracking=0.3),
            "cell": style("c", 9.5, 14, ink, align=2),
            "due_label": style("dl", 9, 13, paper, align=2, tracking=0.6),
            "due": style("due", 16, 20, paper, align=2),
            "foot": style("f", 8, 12, muted, align=1),
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

    def _brand_band(self, st, colors, seller: dict, kind_ar: str):
        """A full-width accent masthead: the seller's name reversed out on the brand colour with the
        document KIND named in Arabic beneath it. It is the single strongest 'this is a real, premium
        document' signal, and it costs one table."""
        name = self._auto(seller.get("name", self._brand.get("name", "")))
        band = self._rl.Table(
            [[[self._rl.Paragraph(name, st["brand"]),
               self._rl.Spacer(1, 3),
               self._rl.Paragraph(self._ar(kind_ar), st["brand_kind"])]]],
            colWidths=[499])
        band.setStyle(self._rl.TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.Color(*self._accent)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 20), ("RIGHTPADDING", (0, 0), (-1, -1), 20),
            ("TOPPADDING", (0, 0), (-1, -1), 18), ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ]))
        return band

    def _field(self, st, label_ar: str, value_markup: str, value_style: str = "value") -> list:
        """A right-aligned label above its value — the atom the header and party blocks are built of."""
        return [self._rl.Paragraph(self._ar(label_ar), st["label"]),
                self._rl.Paragraph(value_markup, st[value_style])]

    def _party_block(self, st, title_ar: str, party: dict) -> list:
        rows = [[self._rl.Paragraph(self._ar(title_ar), st["label"])]]
        name = self._auto(party.get("name", ""))
        if name:
            rows.append([self._rl.Paragraph(name, st["value"])])
        elif party.get("account"):
            # No named buyer (a simplified B2C invoice): show the tenant as an ACCOUNT reference,
            # clearly labelled — never as a bare legal name, which a raw id is not.
            rows.append([self._rl.Paragraph(self._ar("الحساب"), st["label"])])
            rows.append([self._rl.Paragraph(self._auto(party.get("account")), st["sub"])])
        for key, label in (("vat_number", "الرقم الضريبي"), ("cr_number", "السجل التجاري"),
                           ("address", "")):
            val = party.get(key, "")
            if val:
                # The « · » separator sits OUTSIDE the Arabic run (Latin face), so it renders and
                # gives the label a clear gap from the number instead of jamming against it.
                text = f"{self._ar(label)} · {self._auto(val)}" if label else self._auto(val)
                # LTR base direction on these mixed label+number lines; pure Arabic uses st["sub"].
                rows.append([self._rl.Paragraph(text, st["sub_x"] if label else st["sub"])])
        return rows

    def _lines_table(self, st, invoice: dict, colors):
        cur = invoice.get("currency", "")
        lines = list(invoice.get("lines", []) or [])
        truncated = len(lines) > self._max_lines
        if truncated:
            lines = lines[: self._max_lines]
        vat_share = apportion_vat(lines, tax_minor=int(invoice.get("tax_minor", 0) or 0),
                                  taxable_minor=int(invoice.get("subtotal_minor", 0) or 0))

        # Columns read right-to-left: description on the right (where Arabic reading begins), amount
        # on the left. Everything numeric stays on the Latin face via money()/_auto().
        head = [self._rl.Paragraph(self._ar("المبلغ"), st["th"]),
                self._rl.Paragraph(self._ar("الضريبة"), st["th"]),
                self._rl.Paragraph(self._ar("السعر"), st["th"]),
                self._rl.Paragraph(self._ar("الكمية"), st["th"]),
                self._rl.Paragraph(self._ar("الوصف"), st["th"])]
        body = [head]
        for line, vat in zip(lines, vat_share):
            body.append([
                self._rl.Paragraph(money(line.get("amount_minor", 0), cur, with_currency=False),
                                   st["cell"]),
                self._rl.Paragraph(money(vat, cur, with_currency=False), st["cell"]),
                self._rl.Paragraph(money(line.get("unit_minor", 0), cur, with_currency=False),
                                   st["cell"]),
                self._rl.Paragraph(self._auto(line.get("quantity", 1)), st["cell"]),
                self._rl.Paragraph(self._auto(line.get("description", "")), st["cell"]),
            ])

        table = self._rl.Table(body, colWidths=[90, 78, 78, 44, 218], repeatRows=1)
        table.setStyle(self._rl.TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), self._font),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            # Hairlines only, and only under rows: a fully boxed grid reads as a spreadsheet.
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, colors.Color(*_INK)),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, colors.Color(*_HAIRLINE)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(*_BAND)]),
        ]))
        return table, truncated

    def _totals(self, st, invoice: dict, colors):
        cur = invoice.get("currency", "")
        rate = percent(invoice.get("vat_rate"))
        # Each row is (label_markup, amount_string). The VAT label carries the rate («… 15%»), so
        # labels are pre-built markup rather than bare words.
        # (label_markup, amount, label_style). The VAT label mixes Arabic with the rate, so it uses
        # the LTR variant; the pure-Arabic labels use the RTL one.
        rows = [(self._ar("المجموع الفرعي"), money(invoice.get("subtotal_minor", 0), cur), "sub")]
        discount = int(invoice.get("discount_minor", 0) or 0)
        if discount:
            rows.append((self._ar("الخصم"), "-" + money(discount, cur), "sub"))
        vat_label = self._ar("ضريبة القيمة المضافة")
        rows.append((f"{vat_label} · {rate}" if rate else vat_label,
                     money(invoice.get("tax_minor", 0), cur), "sub_x" if rate else "sub"))

        data = [[self._rl.Paragraph(val, st["value"]), self._rl.Paragraph(label, st[lstyle])]
                for label, val, lstyle in rows]
        # The amount due is the one number that matters, so it gets the accent — a filled panel with
        # the total reversed out, labelled «الإجمالي المستحق».
        data.append([self._rl.Paragraph(money(invoice.get("total_minor", 0), cur), st["due"]),
                     self._rl.Paragraph(self._ar("الإجمالي المستحق"), st["due_label"])])

        table = self._rl.Table(data, colWidths=[128, 190], hAlign="LEFT")
        table.setStyle(self._rl.TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), self._font),
            ("TOPPADDING", (0, 0), (-1, -2), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -2), 5),
            ("BACKGROUND", (0, -1), (-1, -1), colors.Color(*self._accent)),
            ("TOPPADDING", (0, -1), (-1, -1), 11),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 11),
            ("LEFTPADDING", (0, -1), (0, -1), 12),
            ("RIGHTPADDING", (-1, -1), (-1, -1), 12),
            ("VALIGN", (0, -1), (-1, -1), "MIDDLE"),
        ]))
        return table

    def _chrome(self, invoice: dict, seller: dict):
        """The per-page footer, drawn on EVERY page so page two is still identifiably this document.

        Arabic «صفحة» is drawn on the Arabic face; the number and the identifier stay Latin."""
        number = str(invoice.get("number", ""))
        ident = f"{number} · {seller.get('name', '')}"[:90]

        def _draw(canvas, document):
            canvas.saveState()
            canvas.setFillColorRGB(*_MUTED)
            canvas.setFont(self._font, 7.5)
            canvas.drawString(48, 34, ident)
            page = str(canvas.getPageNumber())
            if self._arabic:
                label = _shape("صفحة")
                canvas.setFont(self._arabic, 7.5)
                canvas.drawRightString(547, 34, label)
                w = canvas.stringWidth(label, self._arabic, 7.5)
                canvas.setFont(self._font, 7.5)
                canvas.drawRightString(547 - w - 3, 34, page)
            else:
                canvas.drawRightString(547, 34, page)
            canvas.setStrokeColorRGB(*_HAIRLINE)
            canvas.setLineWidth(0.4)
            canvas.line(48, 48, 547, 48)
            canvas.restoreState()

        return _draw

    def _doc(self, buf, *, title: str):
        return self._rl.SimpleDocTemplate(
            buf, pagesize=(595, 842), leftMargin=48, rightMargin=48, topMargin=46, bottomMargin=52,
            title=title, author=self._brand.get("name", ""))

    # ── the tax invoice ─────────────────────────────────────────────────────────
    def render(self, invoice: dict[str, Any]) -> tuple[str, bytes] | None:
        try:
            colors = load("reportlab.lib.colors", "pdf")
            st = self._styles()
            buf = io.BytesIO()
            doc = self._doc(buf, title=f"Invoice {invoice.get('number', '')}")

            seller = invoice.get("seller") or self._brand.get("seller") or {}
            buyer = invoice.get("buyer") or {"account": invoice.get("tenant", "")}
            simplified = not (buyer.get("vat_number"))
            kind_ar = "فاتورة ضريبية مبسطة" if simplified else "فاتورة ضريبية"

            P = self._rl.Paragraph
            story: list = [self._brand_band(st, colors, seller, kind_ar), self._rl.Spacer(1, 18)]

            # The invoice's own identity: number on the right (where reading begins), date on the left.
            head = self._rl.Table(
                [[self._field(st, "تاريخ الإصدار",
                              self._issued(invoice.get("created", "") or invoice.get("paid_at", "")),
                              value_style="value_x"),
                  self._field(st, "رقم الفاتورة", self._auto(invoice.get("number", "")))]],
                colWidths=[249, 250])
            head.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [head, self._rl.Spacer(1, 20)]

            parties = self._rl.Table(
                [[self._party_block(st, "إلى", buyer), self._party_block(st, "من", seller)]],
                colWidths=[249, 250])
            parties.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [parties, self._rl.Spacer(1, 22)]

            table, truncated = self._lines_table(st, invoice, colors)
            story += [table, self._rl.Spacer(1, 16)]
            if truncated:
                story.append(P(self._auto(f"عرض أول {self._max_lines} من "
                                          f"{len(invoice.get('lines', []))} بند."), st["sub"]))

            qr = self._qr_flowable(invoice)
            footer_left = [qr, P(self._ar("رمز الاستجابة السريعة"), st["label"])] if qr \
                else [P("", st["sub"])]
            summary = self._rl.Table([[footer_left, self._totals(st, invoice, colors)]],
                                     colWidths=[160, 339])
            summary.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [summary, self._rl.Spacer(1, 26)]

            ein = invoice.get("einvoice") or {}
            trail = " · ".join(x for x in (
                f"UUID {_safe(ein.get('uuid', ''))}" if ein.get("uuid") else "",
                f"ICV {_safe(ein.get('icv', ''))}" if ein.get("icv") else "") if x)
            if trail:
                # The e-invoicing identifiers, discreetly. An auditor reconciling a printed invoice
                # against the ZATCA submission needs them.
                story.append(P(trail, st["foot"]))

            chrome = self._chrome(invoice, seller)
            doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
            return "application/pdf", buf.getvalue()
        except Exception as exc:  # noqa: BLE001 — a document must never stop the money being recorded
            _log.error("invoice_pdf_render_failed",
                       extra={"invoice": invoice.get("number", ""), "error": type(exc).__name__})
            return None

    # ── the internal receipt (non-tax) ──────────────────────────────────────────
    def render_receipt(self, invoice: dict[str, Any], payment: dict[str, Any]
                       ) -> tuple[str, bytes] | None:
        """A payment RECEIPT (سند قبض) — confirms money received, carries the gateway's transaction
        reference, and is NOT a tax document: no VAT lines, no seller VAT number, no ZATCA QR. It is
        the default document a platform issues before it opts into tax invoicing, and it works for
        every gateway because it needs nothing from the gateway but the reference we already stored."""
        try:
            colors = load("reportlab.lib.colors", "pdf")
            st = self._styles()
            buf = io.BytesIO()
            doc = self._doc(buf, title=f"Receipt {invoice.get('number', '')}")
            seller = invoice.get("seller") or self._brand.get("seller") or {}
            buyer = invoice.get("buyer") or {"account": invoice.get("tenant", "")}
            cur = invoice.get("currency", "")
            P = self._rl.Paragraph
            story: list = [self._brand_band(st, colors, seller, "سند قبض"), self._rl.Spacer(1, 18)]

            paid_at = payment.get("paid_at") or invoice.get("paid_at", "") or invoice.get("created", "")
            head = self._rl.Table(
                [[self._field(st, "تاريخ الدفع", self._issued(paid_at), value_style="value_x"),
                  self._field(st, "رقم الإيصال", self._auto(invoice.get("number", "")))]],
                colWidths=[249, 250])
            head.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [head, self._rl.Spacer(1, 20)]

            parties = self._rl.Table(
                [[self._party_block(st, "المدفوع من", buyer), self._party_block(st, "من", seller)]],
                colWidths=[249, 250])
            parties.setStyle(self._rl.TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [parties, self._rl.Spacer(1, 20)]

            # What it was for — a plain description, never a taxed line table.
            desc = self._auto(invoice.get("description")
                              or (invoice.get("lines") or [{}])[0].get("description", "")
                              or f"فاتورة {invoice.get('number', '')}")
            story += [P(self._ar("مقابل"), st["label"]), P(desc, st["value"]), self._rl.Spacer(1, 14)]

            # The AMOUNT PAID panel (the one number that matters) + the PAYMENT REFERENCE beside it.
            amount = self._rl.Table(
                [[P(money(invoice.get("total_minor", 0), cur), st["due"]),
                  P(self._ar("المبلغ المدفوع"), st["due_label"])]],
                colWidths=[128, 190], hAlign="LEFT")
            amount.setStyle(self._rl.TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), self._font),
                ("BACKGROUND", (0, 0), (-1, -1), colors.Color(*self._accent)),
                ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
                ("LEFTPADDING", (0, 0), (0, 0), 12), ("RIGHTPADDING", (-1, 0), (-1, 0), 12),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
            story += [self._rl.Table([[self._payment_ref(st, payment), amount]], colWidths=[160, 339],
                                     style=self._rl.TableStyle([
                                         ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                         ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                         ("RIGHTPADDING", (0, 0), (-1, -1), 0)])),
                      self._rl.Spacer(1, 24),
                      P(self._ar("هذا إيصال دفع وليس فاتورة ضريبية"), st["foot"])]

            chrome = self._chrome(invoice, seller)
            doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
            return "application/pdf", buf.getvalue()
        except Exception as exc:  # noqa: BLE001 — a document must never stop the money being recorded
            _log.error("receipt_pdf_render_failed",
                       extra={"invoice": invoice.get("number", ""), "error": type(exc).__name__})
            return None

    def _payment_ref(self, st, payment: dict[str, Any]) -> list:
        """The gateway (or settlement) reference — the traceable link to how the money arrived."""
        rows = [[self._rl.Paragraph(self._ar("مرجع الدفع"), st["label"])]]
        provider = payment.get("provider", "")
        pid = payment.get("provider_id", "")
        method = payment.get("method", "")
        reference = payment.get("reference", "")
        if provider or pid:
            rows.append([self._rl.Paragraph(self._auto(f"{provider} · {pid}".strip(" ·")), st["sub"])])
        elif method or reference:
            # An off-gateway settlement (wire/cheque): show its method + bank reference instead.
            rows.append([self._rl.Paragraph(self._auto(f"{method} · {reference}".strip(" ·")),
                                            st["sub"])])
        return rows
