"""Regression tests for zatca-service audit findings.

#02 the signer is resolved PER TENANT (signer_provider), so a sweep spanning tenants signs each
    invoice with its own credential instead of one request-wide token.
#04 a malformed invoice payload is a clean 422 that burns no ICV, not an unhandled 500.

#01 the PIH is bound at SIGN time and signing is strictly in ICV order, so deferred/out-of-order
    signing can no longer fork the chain.
#03 the retry sweep is O(due): capped invoices go terminal and it stops at the first not-yet-due row.
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


# ── 01: out-of-order signing keeps the PIH/ICV chain intact ────────────────────
import time as _time  # noqa: E402

from app.ports.zatca_signer import SignerUnavailable  # noqa: E402


class _FlakySigner:
    """Signs, but raises SignerUnavailable for the first `fail_first` calls — to force a deferral so a
    later ICV is finalized while an earlier one is still unsigned (the zatca-01 fork scenario)."""
    driver = "flaky"

    def __init__(self, fail_first: int = 1):
        self.calls = 0
        self.fail_first = fail_first
        self.n = 0

    def sign(self, **k):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise SignerUnavailable("signer down")
        self.n += 1
        # hash must depend on the (corrected) PIH so a wrong PIH would be a wrong, detectable chain
        import hashlib
        pih = k["invoice_xml"].split('mimeCode="text/plain">')[1].split("<")[0]
        h = hashlib.sha256((pih + str(self.n)).encode()).hexdigest()
        return {"signedXml": f"<s icv='{self.n}'/>", "invoiceHash": h, "qrBase64": "Q",
                "signatureBase64": "G", "signingTimeUtc": "T"}


def test_out_of_order_signing_keeps_the_chain_intact():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=_FlakySigner(fail_first=1), fatoora=_MockFatoora(), qr=NullQr(),
                     vat_rate="0.15", currency="SAR", retry=RETRY)
    # ICV 1's first sign FAILS -> stays pending. ICV 2 is then finalized while ICV 1 is unsigned:
    # it must DEFER (not sign onto a stale PIH), the pre-fix fork.
    r1 = run(z.finalize({**INV, "number": "INV-1"}, "t1"))
    r2 = run(z.finalize({**INV, "number": "INV-2"}, "t1"))
    assert r1["status"] == "pending" and r2["status"] == "pending"
    id1 = r1["invoice_id"]; id2 = r2["invoice_id"]
    # Attempt ICV 2 FIRST (out of order): it must DEFER rather than sign onto a stale/absent PIH.
    run(z.resubmit(id2))
    assert run(InvoiceStore(st).get(id2)).status == "pending"           # refused to sign out of order
    # Now ICV 1 signs, then ICV 2 signs — chaining onto ICV 1's REAL hash.
    run(z.resubmit(id1))
    run(z.resubmit(id2))
    i1 = run(InvoiceStore(st).get(id1))
    i2 = run(InvoiceStore(st).get(id2))
    assert i1.icv == 1 and i2.icv == 2
    assert i2.pih == i1.hash                                             # ICV 2 chains onto ICV 1's REAL hash
    assert run(z.verify_chain("t1"))["ok"] is True                      # pre-fix: pih_mismatch at ICV 2


# ── 03: retry sweep is O(due) — capped invoices leave the swept set ────────────
def test_capped_invoice_becomes_terminal_and_leaves_the_sweep():
    st = MemoryDataStore()
    _onboard(st, "t1")
    # max_attempts=1 so one failed attempt caps it. Signer always down -> deferral then cap.
    class _Down:
        driver = "down"
        def sign(self, **k):
            raise SignerUnavailable("down")
    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=_Down(), fatoora=_MockFatoora(), qr=NullQr(), vat_rate="0.15",
                     currency="SAR", retry={"base_ms": 1, "max_ms": 2, "max_attempts": 1})
    run(z.finalize(INV, "t1"))
    inv = run(InvoiceStore(st).by_number("t1", "INV-1"))
    # first defer scheduled a retry; drive it until capped
    for _ in range(5):
        due = run(InvoiceStore(st).due_for_retry(_time.time() + 10_000, 100))
        if not due:
            break
        run(z.sweep())
    inv = run(InvoiceStore(st).by_number("t1", "INV-1"))
    assert inv.status == "failed"                                        # terminal, not stuck 'pending'
    # a capped/terminal invoice is no longer read by the retry sweep, even far in the future
    assert run(InvoiceStore(st).due_for_retry(_time.time() + 10_000, 100)) == []


def test_due_for_retry_stops_at_first_not_due():
    st = MemoryDataStore()
    now = 1_000_000.0
    ist = InvoiceStore(st)
    from app.core.models import ZatcaInvoice, new_id
    # two due (past), one far-future — the sweep must not have to read past the future one
    for n, nra, status in [("A", now - 10, "pending"), ("B", now - 5, "signed"),
                           ("C", now + 10_000, "pending")]:
        run(ist.save(ZatcaInvoice(id=new_id(), tenant="t1", number=n, icv=0, status=status,
                                  next_retry_at=nra, created=f"2026-01-0{ord(n)-64}T00:00:00")))
    due = run(ist.due_for_retry(now, 100))
    assert {i.number for i in due} == {"A", "B"}                         # only the due ones, C excluded
