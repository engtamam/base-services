"""Generic UBL 2.1 tax-invoice assembly (the UNSIGNED XML).

General-purpose (any seller/buyer/currency/VAT rate/lines) — NOT tied to one platform's
record shape. Owns only the two SDK-independent pieces: the UBL body and the PIH/ICV
chain slot. It OMITS ext:UBLExtensions, cac:Signature and the QR node — the signer
(official SDK) adds those. Money is integer minor units (e.g. halalas); no float drift.

InvoiceTypeCode name: 0100000 standard (B2B clearance) · 0200000 simplified (B2C
reporting). Type is chosen upstream from the buyer: standard when the buyer has a VAT
number, simplified otherwise.
"""
from __future__ import annotations

import time
import uuid as _uuid
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape as _esc


def _major(minor: int, places: int = 2) -> str:
    return f"{Decimal(int(minor)) / (10 ** places):.{places}f}"


def _vat_of(net_minor: int, rate: str) -> int:
    return int((Decimal(int(net_minor)) * Decimal(str(rate))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def stable_uuid(number: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"urn:base-services:zatca:invoice:{number}"))


import re as _re

# The PIH lives in the one EmbeddedDocumentBinaryObject in the unsigned UBL (the QR node is added by
# the signer afterwards). This targets exactly that element's text so the PIH can be corrected at sign
# time without rebuilding the whole document (zatca-01) — the value is base64/hex, no XML specials.
_PIH_RE = _re.compile(
    r"(<cbc:EmbeddedDocumentBinaryObject mimeCode=\"text/plain\">)(.*?)(</cbc:EmbeddedDocumentBinaryObject>)",
    _re.DOTALL)


def set_pih(xml: str, pih: str) -> str:
    """Replace the PIH value in an UNSIGNED UBL document. Used to bind the authoritative PIH at sign
    time. Raises if the PIH node is absent (a document that cannot carry a chain link must not sign)."""
    new, n = _PIH_RE.subn(lambda m: m.group(1) + _esc(str(pih)) + m.group(3), xml, count=1)
    if n != 1:
        raise ValueError("PIH node not found in unsigned UBL")
    return new


def build_ubl(inv: dict, *, icv: int, pih: str, invoice_type: str = "simplified") -> dict:
    """Assemble the unsigned UBL 2.1 invoice + rolled-up money figures.

    inv: {number, uuid?, issued_at (epoch s), currency, vat_rate, seller{name,vat_number,
    address}, buyer{name,vat_number?,id?}, lines[{description, qty, amount_minor (NET)}]}.
    Returns {xml, uuid, tax_exclusive, vat, tax_inclusive}.
    """
    t = time.gmtime(int(inv.get("issued_at") or 0))
    issue_date, issue_time = time.strftime("%Y-%m-%d", t), time.strftime("%H:%M:%S", t)
    uuid_v = inv.get("uuid") or stable_uuid(str(inv["number"]))
    seller = inv.get("seller") or {}
    buyer = inv.get("buyer") or {}
    cur = str(inv.get("currency", "SAR")).upper()
    if not (len(cur) == 3 and cur.isalpha()):
        # currencyID is interpolated into signed XML attributes; an ISO-4217 code is 3 letters, so
        # reject anything else rather than let it inject markup into the document we sign (CWE-91).
        raise ValueError(f"invalid currency code: {inv.get('currency')!r}")
    rate = str(inv.get("vat_rate", "0.15"))
    # Percent for the XML (e.g. "0.15" -> "15"). Decimal, not float(rate)*100, so an odd rate never
    # picks up a binary tail before rounding (money-adjacent value on a signed document).
    vat_pct = str(int((Decimal(rate) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    type_name = "0200000" if invoice_type == "simplified" else "0100000"
    profile_id = "reporting:1.0" if invoice_type == "simplified" else "clearance:1.0"

    lines = inv.get("lines") or []
    nets = [int(ln["amount_minor"]) for ln in lines]
    vats = [_vat_of(n, rate) for n in nets]            # per-line half-up VAT (line-level TaxTotals)
    line_extension = sum(nets)
    # CATEGORY / document VAT = round(Σ taxable × rate), NOT the sum of per-line rounded VATs. ZATCA
    # validates the category TaxAmount as round(category_taxable × rate) (BR-S-08 / BR-CO-14); summing
    # per-line half-up rounding drifts by a halala on multi-line invoices and the invoice is rejected.
    vat = _vat_of(line_extension, rate)
    gross = line_extension + vat

    line_xml = "\n".join(
        f"""  <cac:InvoiceLine>
    <cbc:ID>{i + 1}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="PCE">{int(lines[i].get('qty', 1))}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{_major(nets[i])}</cbc:LineExtensionAmount>
    <cac:TaxTotal>
      <cbc:TaxAmount currencyID="{cur}">{_major(vats[i])}</cbc:TaxAmount>
      <cbc:RoundingAmount currencyID="{cur}">{_major(nets[i] + vats[i])}</cbc:RoundingAmount>
    </cac:TaxTotal>
    <cac:Item>
      <cbc:Name>{_esc(str(lines[i].get('description', '')))}</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>{vat_pct}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="{cur}">{_major(nets[i])}</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>"""
        for i in range(len(lines))
    )

    buyer_vat = (
        f"""    <cac:PartyTaxScheme>
      <cbc:CompanyID>{_esc(str(buyer['vat_number']))}</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:PartyTaxScheme>
""" if buyer.get("vat_number") else ""
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
         xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">
  <cbc:ProfileID>{profile_id}</cbc:ProfileID>
  <cbc:ID>{_esc(str(inv['number']))}</cbc:ID>
  <cbc:UUID>{_esc(str(uuid_v))}</cbc:UUID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  <cbc:IssueTime>{issue_time}</cbc:IssueTime>
  <cbc:InvoiceTypeCode name="{type_name}">388</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>
  <cbc:TaxCurrencyCode>{cur}</cbc:TaxCurrencyCode>
  <cac:AdditionalDocumentReference>
    <cbc:ID>ICV</cbc:ID>
    <cbc:UUID>{int(icv)}</cbc:UUID>
  </cac:AdditionalDocumentReference>
  <cac:AdditionalDocumentReference>
    <cbc:ID>PIH</cbc:ID>
    <cac:Attachment>
      <cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{_esc(str(pih))}</cbc:EmbeddedDocumentBinaryObject>
    </cac:Attachment>
  </cac:AdditionalDocumentReference>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PostalAddress>
        <cbc:StreetName>{_esc(str(seller.get('address', '') or 'N/A'))}</cbc:StreetName>
        <cac:Country><cbc:IdentificationCode>SA</cbc:IdentificationCode></cac:Country>
      </cac:PostalAddress>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>{_esc(str(seller.get('vat_number', '') or ''))}</cbc:CompanyID>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:PartyTaxScheme>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{_esc(str(seller.get('name', '') or 'N/A'))}</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="ORG">{_esc(str(buyer.get('id', '') or ''))}</cbc:ID>
      </cac:PartyIdentification>
{buyer_vat}      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{_esc(str(buyer.get('name', '') or 'Customer'))}</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="{cur}">{_major(vat)}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{cur}">{_major(line_extension)}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{cur}">{_major(vat)}</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>{vat_pct}</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{cur}">{_major(line_extension)}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{_major(line_extension)}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{_major(gross)}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="{cur}">{_major(gross)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
{line_xml}
</Invoice>"""

    return {"xml": xml, "uuid": str(uuid_v), "tax_exclusive": _major(line_extension),
            "vat": _major(vat), "tax_inclusive": _major(gross)}
