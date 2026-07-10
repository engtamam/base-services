"""Unit: UBL, chain, classify, backoff, seal, and the orchestration state machine."""
from __future__ import annotations

import asyncio
import xml.dom.minidom as _md

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.fatoora.null import NullFatoora
from app.adapters.qr.null import NullQr
from app.adapters.signer.null import NullSigner
from app.core.backoff import compute_retry_schedule
from app.core.chain import GENESIS_PIH, verify_chain
from app.core.classify import classify
from app.core.seal import seal, unseal
from app.core.stores import InvoiceStore, SettingsStore
from app.core.ubl import build_ubl
from app.core.zatca_service import ZatcaService

RETRY = {"base_ms": 60_000, "max_ms": 21_600_000, "max_attempts": 12}
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def run(c):
    return asyncio.run(c)


# ── pure ─────────────────────────────────────────────────────────────────────
def test_ubl_types_and_vat():
    b = build_ubl({**INV, "vat_rate": "0.15", "currency": "SAR"}, icv=1, pih=GENESIS_PIH,
                  invoice_type="simplified")
    _md.parseString(b["xml"])                                  # well-formed
    assert "0200000" in b["xml"] and "reporting:1.0" in b["xml"]
    assert b["tax_exclusive"] == "100.00" and b["vat"] == "15.00" and b["tax_inclusive"] == "115.00"
    std = build_ubl({**INV, "buyer": {"vat_number": "311"}}, icv=2, pih="H", invoice_type="standard")
    assert "0100000" in std["xml"] and "clearance:1.0" in std["xml"]


def test_chain_breaks():
    good = [{"icv": 1, "pih": GENESIS_PIH, "hash": "H1", "status": "reported"},
            {"icv": 2, "pih": "H1", "hash": "H2", "status": "pending"}]
    assert verify_chain(good)["ok"]                            # pending tail allowed
    assert verify_chain([{"icv": 1, "pih": "X", "hash": "H1", "status": "reported"}])["reason"] == "genesis_mismatch"
    assert verify_chain([{"icv": 1, "pih": GENESIS_PIH, "hash": "H1", "status": "reported"},
                         {"icv": 2, "pih": "BAD", "hash": "H2", "status": "reported"}])["reason"] == "pih_mismatch"
    assert verify_chain([{"icv": 1, "pih": GENESIS_PIH, "hash": "H1", "status": "reported"},
                         {"icv": 3, "pih": "H1", "hash": "H3", "status": "reported"}])["reason"] == "icv_gap"
    assert verify_chain([{"icv": 1, "pih": GENESIS_PIH, "hash": "", "status": "pending"},
                         {"icv": 2, "pih": "", "hash": "H2", "status": "reported"}])["reason"] == "unsigned_link"


def test_classify_table():
    assert classify(200, {"reportingStatus": "REPORTED"}, success_status="reported")["outcome"] == "reported"
    warn = classify(202, {"reportingStatus": "REPORTED", "validationResults": {"warningMessages": [{"message": "w"}]}},
                    success_status="reported")
    assert warn["outcome"] == "reported" and warn["warnings"] == ["w"]
    assert classify(400, {"validationResults": {"errorMessages": [{"message": "e"}]}},
                    success_status="reported")["outcome"] == "rejected"
    assert classify(413, {}, success_status="cleared")["outcome"] == "rejected"
    for c in (401, 429, 500, 503, 0):
        assert classify(c, {}, success_status="reported")["outcome"] == "pending"


def test_backoff_escalates_and_caps():
    assert [compute_retry_schedule(a, **RETRY)["next_delay_ms"] for a in (0, 1, 2, 3)] == \
        [60_000, 120_000, 240_000, 480_000]
    assert compute_retry_schedule(12, **RETRY) == {"next_delay_ms": None, "capped": True}
    assert compute_retry_schedule(-5, **RETRY)["next_delay_ms"] == 60_000


def test_seal_roundtrip():
    tok = seal("secret-key-material", "master")
    assert tok and "secret-key-material" not in tok
    assert unseal(tok, "master") == "secret-key-material"
    assert unseal(tok, "wrong") is None


# ── orchestration ─────────────────────────────────────────────────────────────
class MockSigner:
    driver = "mock"

    def __init__(self):
        self.n = 0

    def sign(self, **k):
        self.n += 1
        return {"signedXml": "<s/>", "invoiceHash": f"HASH{self.n}", "qrBase64": "Q",
                "signatureBase64": "G", "signingTimeUtc": "T"}


class MockFatoora:
    driver = "mock"

    def report(self, **k):
        return 200, {"reportingStatus": "REPORTED"}

    def clear(self, **k):
        return 200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<stamped/>"}


def _svc(store, signer, fatoora):
    return ZatcaService(invoices=InvoiceStore(store), settings=SettingsStore(store, "k" * 8),
                        signer=signer, fatoora=fatoora, qr=NullQr(), vat_rate="0.15",
                        currency="SAR", retry=RETRY)


def _onboarded(store):
    s = SettingsStore(store, "k" * 8)
    run(s.store_csid("t1", certificate_pem="C", secret="S", private_key_pem="K"))
    return s


def test_finalize_fail_closed_keeps_pending_no_icv_waste():
    st = MemoryDataStore()
    _onboarded(st)
    z = _svc(st, NullSigner(), NullFatoora())
    r1 = run(z.finalize(INV, "t1"))
    assert r1["status"] == "pending" and r1["icv"] == 1
    r2 = run(z.finalize(INV, "t1"))                            # idempotent — same slot
    assert r2["icv"] == 1


def test_finalize_reported_and_chain():
    st = MemoryDataStore()
    _onboarded(st)
    z = _svc(st, MockSigner(), MockFatoora())
    assert run(z.finalize(INV, "t1"))["status"] == "reported"
    assert run(z.verify_chain("t1"))["ok"]


def test_finalize_standard_is_cleared():
    st = MemoryDataStore()
    _onboarded(st)
    z = _svc(st, MockSigner(), MockFatoora())
    r = run(z.finalize({**INV, "buyer": {"name": "B", "vat_number": "311"}}, "t1"))
    assert r["status"] == "cleared" and r["invoice_type"] == "standard"


def test_hard_reject_is_terminal():
    class Rej(MockFatoora):
        def report(self, **k):
            return 400, {"validationResults": {"errorMessages": [{"message": "BR-KSA-99"}]}}

    st = MemoryDataStore()
    _onboarded(st)
    r = run(_svc(st, MockSigner(), Rej()).finalize(INV, "t1"))
    assert r["status"] == "rejected" and r["errors"] == ["BR-KSA-99"]


def test_sweep_retries_after_recovery():
    st = MemoryDataStore()
    _onboarded(st)
    # transient fatoora first → pending; then a healthy sweep clears it
    class Down(MockFatoora):
        def report(self, **k):
            return 503, {}
    z = _svc(st, MockSigner(), Down())
    run(z.finalize(INV, "t1"))
    rec = [r for r in run(InvoiceStore(st)._rows("t1"))][0]
    assert rec["status"] in ("signed", "submitted") and rec["next_retry_at"] is not None
    # force it due, then sweep with a healthy client
    rec["next_retry_at"] = 0
    run(st.put("zatca_invoices", rec["id"], rec))
    z2 = _svc(st, MockSigner(), MockFatoora())
    assert run(z2.sweep())["processed"] == 1
    assert run(z2.verify_chain("t1"))["ok"]


def test_tamper_detected():
    st = MemoryDataStore()
    _onboarded(st)
    z = _svc(st, MockSigner(), MockFatoora())
    run(z.finalize(INV, "t1"))
    run(z.finalize({**INV, "number": "INV-2"}, "t1"))
    assert run(z.verify_chain("t1"))["ok"]
    rows = sorted(run(InvoiceStore(st)._rows("t1")), key=lambda r: r["icv"])
    rows[0]["hash"] = "TAMPERED"
    run(st.put("zatca_invoices", rows[0]["id"], rows[0]))
    bad = run(z.verify_chain("t1"))
    assert bad["ok"] is False and bad["reason"] == "pih_mismatch" and bad["break_at"] == 2


def test_onboarding_compliance_then_production():
    class OnbSigner(MockSigner):
        def generate_csr(self, meta):
            return {"csrPem": "CSR", "privateKeyPem": "PK", "certificateTemplate": "T", "env": "sim"}

    class OnbFatoora(MockFatoora):
        def onboard_compliance(self, *, csr_pem, otp):
            return 200, {"binarySecurityToken": "CCSID", "secret": "CSEC", "requestID": "REQ-1"}

        def onboard_production(self, *, compliance_request_id, csid_token, csid_secret):
            assert compliance_request_id == "REQ-1"
            return 200, {"binarySecurityToken": "PCSID", "secret": "PSEC"}

    st = MemoryDataStore()
    z = _svc(st, OnbSigner(), OnbFatoora())
    c = run(z.onboard_compliance("t1", otp="123456", csr_meta={}))
    assert c["onboarding_status"] == "compliance" and c["request_id"] == "REQ-1"
    p = run(z.onboard_production("t1"))
    assert p["onboarding_status"] == "production"
    assert run(z._set.view("t1"))["onboarded"] is True
    # production without compliance first -> failed, not a crash
    assert run(_svc(MemoryDataStore(), OnbSigner(), OnbFatoora()).onboard_production("nope"))["onboarding_status"] == "failed"


def test_pre_submit_validation_blocks_invalid():
    class BadValidateSigner(MockSigner):
        def validate(self, xml):
            return {"status": "invalid", "detail": "schematron fail"}

    calls = {"submitted": 0}

    class CountFatoora(MockFatoora):
        def report(self, **k):
            calls["submitted"] += 1
            return 200, {"reportingStatus": "REPORTED"}

    st = MemoryDataStore()
    _onboarded(st)
    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=BadValidateSigner(), fatoora=CountFatoora(), qr=NullQr(),
                     vat_rate="0.15", currency="SAR", retry=RETRY, validate_before_submit=True)
    r = run(z.finalize(INV, "t1"))
    assert r["status"] == "rejected" and calls["submitted"] == 0     # never submitted an invalid invoice


def test_pre_submit_validation_fails_open_when_validator_down():
    class NoValidateSigner(MockSigner):
        def validate(self, xml):
            from app.ports.zatca_signer import SignerUnavailable
            raise SignerUnavailable("validator down")

    st = MemoryDataStore()
    _onboarded(st)
    z = ZatcaService(invoices=InvoiceStore(st), settings=SettingsStore(st, "k" * 8),
                     signer=NoValidateSigner(), fatoora=MockFatoora(), qr=NullQr(),
                     vat_rate="0.15", currency="SAR", retry=RETRY, validate_before_submit=True)
    assert run(z.finalize(INV, "t1"))["status"] == "reported"        # don't block a valid invoice


def test_allocate_slot_is_o1_via_index():
    st = MemoryDataStore()
    _onboarded(st)
    z = _svc(st, MockSigner(), MockFatoora())
    run(z.finalize(INV, "t1"))
    run(z.finalize({**INV, "number": "INV-2"}, "t1"))
    inv2 = run(InvoiceStore(st).by_number("t1", "INV-2"))
    assert inv2.icv == 2                                             # head advanced, index resolves
    head = run(st.get("zatca_index", "head::t1"))
    assert head["reserved_icv"] == 2 and head["last_hash"]
    assert run(st.get("zatca_index", "num::t1::INV-1")) is not None


def test_tenant_isolation():
    st = MemoryDataStore()
    s = SettingsStore(st, "k" * 8)
    for t in ("t1", "t2"):
        run(s.store_csid(t, certificate_pem="C", secret="S", private_key_pem="K"))
    z = _svc(st, MockSigner(), MockFatoora())
    run(z.finalize(INV, "t1"))
    assert run(z.verify_chain("t2"))["count"] == 0
