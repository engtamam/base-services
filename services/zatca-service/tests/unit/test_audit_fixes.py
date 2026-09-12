"""Regression tests for zatca-service audit findings.

#02 the signer is resolved PER TENANT (signer_provider), so a sweep spanning tenants signs each
    invoice with its own credential instead of one request-wide token.
#04 a malformed invoice payload is a clean 422 that burns no ICV, not an unhandled 500.

(#01 chain-PIH binding and #03 sweep-scan scope are surfaced to the operator as NEEDS_DECISION —
they change the ZATCA chain/lifecycle of the invoice, which is held off-limits.)
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.fatoora.null import NullFatoora
from app.adapters.qr.null import NullQr
from app.core.stores import InvoiceStore, SettingsStore
from app.core.zatca_service import ZatcaInputError, ZatcaService

RETRY = {"base_ms": 60_000, "max_ms": 21_600_000, "max_attempts": 12}
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def run(c):
    return asyncio.run(c)


class _TenantSigner:
    """A signer that records which credential (per-tenant token) it was built with, so a test can
    assert each invoice was signed with ITS tenant's token."""
    driver = "mock"

    def __init__(self, token):
        self.token = token

    def sign(self, **k):
        return {"signedXml": f"<s tok='{self.token}'/>", "invoiceHash": f"H:{self.token}",
                "qrBase64": "Q", "signatureBase64": "G", "signingTimeUtc": "T"}


class _MockFatoora(NullFatoora):
    def report(self, **k):
        return 200, {"reportingStatus": "REPORTED"}

    def clear(self, **k):
        return 200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<stamped/>"}


def _onboard(store, *tenants):
    s = SettingsStore(store, "k" * 8)
    for t in tenants:
        run(s.store_csid(t, certificate_pem="C", secret="S", private_key_pem="K"))
    return s


# ── 02: the signer is resolved per tenant, not once per request ───────────────
def test_sweep_signs_each_tenant_with_its_own_credential():
    st = MemoryDataStore()
    _onboard(st, "t1", "t2")
    seen = {}

    async def provider(tenant):
        signer = _TenantSigner(f"tok::{tenant}")
        return signer

    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=_TenantSigner("tok::PLATFORM"), fatoora=_MockFatoora(), qr=NullQr(),
                     vat_rate="0.15", currency="SAR", retry=RETRY, signer_provider=provider)
    run(z.finalize(INV, "t1"))
    run(z.finalize({**INV, "number": "INV-2"}, "t2"))
    # Each invoice's stored hash carries the token of ITS tenant (pre-fix: both used one request token).
    i1 = run(InvoiceStore(st).by_number("t1", "INV-1"))
    i2 = run(InvoiceStore(st).by_number("t2", "INV-2"))
    assert i1.hash == "H:tok::t1"
    assert i2.hash == "H:tok::t2"


def test_signer_provider_default_falls_back_to_the_fixed_signer():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=_TenantSigner("tok::FIXED"), fatoora=_MockFatoora(), qr=NullQr(),
                     vat_rate="0.15", currency="SAR", retry=RETRY)      # no provider
    run(z.finalize(INV, "t1"))
    assert run(InvoiceStore(st).by_number("t1", "INV-1")).hash == "H:tok::FIXED"


# ── 04: a malformed payload is a controlled error and burns no ICV ────────────
def _svc(st):
    _onboard(st, "t1")
    return ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                        signer=_TenantSigner("tok"), fatoora=_MockFatoora(), qr=NullQr(),
                        vat_rate="0.15", currency="SAR", retry=RETRY)


def test_missing_amount_minor_raises_input_error_not_500():
    st = MemoryDataStore()
    z = _svc(st)
    with pytest.raises(ZatcaInputError):
        run(z.finalize({**INV, "lines": [{"description": "x"}]}, "t1"))   # pre-fix: KeyError -> 500
    # No ICV was reserved for the malformed invoice.
    assert run(InvoiceStore(st).by_number("t1", "INV-1")) is None


def test_bad_currency_raises_input_error():
    st = MemoryDataStore()
    z = _svc(st)
    with pytest.raises(ZatcaInputError):
        run(z.finalize({**INV, "currency": "SARX"}, "t1"))
    assert run(InvoiceStore(st).by_number("t1", "INV-1")) is None


def test_a_valid_invoice_after_a_bad_one_reuses_the_first_icv():
    """The rejected malformed request burned no ICV, so a corrected resend gets ICV 1."""
    st = MemoryDataStore()
    z = _svc(st)
    with pytest.raises(ZatcaInputError):
        run(z.finalize({**INV, "lines": [{"description": "x"}]}, "t1"))
    r = run(z.finalize(INV, "t1"))
    assert r["icv"] == 1                                                 # chain not gapped
