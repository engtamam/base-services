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


def test_category_vat_is_rounded_on_the_total_not_summed_per_line():
    """MEDIUM regression (ZATCA BR-S-08): the category/document TaxAmount must be
    round(Σ taxable × rate), NOT the sum of per-line rounded VATs. Two 0.10 lines: per-line VAT
    rounds 0.015->0.02 each (Σ=0.04), but the category is round(0.20×0.15)=0.03. Summing per-line
    would assert 0.04 against a 0.03 taxable×rate and ZATCA rejects the invoice."""
    b = build_ubl({**INV, "vat_rate": "0.15", "currency": "SAR",
                   "lines": [{"description": "a", "qty": 1, "amount_minor": 10},
                             {"description": "b", "qty": 1, "amount_minor": 10}]},
                  icv=1, pih=GENESIS_PIH, invoice_type="simplified")
    assert b["vat"] == "0.03", f"category VAT must be round(0.20*0.15)=0.03, got {b['vat']}"
    assert b["tax_inclusive"] == "0.23"          # 0.20 + 0.03
    # the document-level TaxAmount in the XML is the category value, not 0.04
    assert '<cbc:TaxAmount currencyID="SAR">0.04</cbc:TaxAmount>' not in b["xml"]


def test_issue_date_is_ksa_local_not_utc():
    """zatca-005: a sale at 01:30 KSA (22:30 UTC the previous day) must be dated on the KSA calendar
    day, not the UTC one. 2023-11-15 01:30:00 +03:00 = epoch 1700000200 (22:30 UTC on 2023-11-14)."""
    epoch = 1_700_000_200          # 2023-11-15 01:16:40 Asia/Riyadh  /  2023-11-14 22:16:40 UTC
    b = build_ubl({**INV, "issued_at": epoch, "vat_rate": "0.15", "currency": "SAR"},
                  icv=1, pih=GENESIS_PIH, invoice_type="simplified")
    assert "<cbc:IssueDate>2023-11-15</cbc:IssueDate>" in b["xml"]     # KSA date, not 2023-11-14
    assert "<cbc:IssueTime>01:16:40</cbc:IssueTime>" in b["xml"]       # KSA time (+3h), not 22:16:40


def test_missing_issued_at_defaults_to_now_never_1970():
    # zatca-006: an absent issued_at must NOT date the signed invoice 1970-01-01 (the `or 0` trap).
    inv = {k: v for k, v in INV.items() if k != "issued_at"}
    b = build_ubl({**inv, "vat_rate": "0.15", "currency": "SAR"}, icv=1, pih=GENESIS_PIH,
                  invoice_type="simplified")
    assert "1970-01-01" not in b["xml"]


def test_explicit_zero_issued_at_is_rejected_before_a_slot():
    # zatca-006: an explicit 0/negative epoch is refused up front (would date the invoice 1970).
    from app.core.zatca_service import _validate_invoice_input, ZatcaInputError
    with pytest.raises(ZatcaInputError):
        _validate_invoice_input({**INV, "issued_at": 0}, "SAR")
    with pytest.raises(ZatcaInputError):
        _validate_invoice_input({**INV, "issued_at": -5}, "SAR")


def test_load_secrets_uses_the_bst_directly_not_double_encoded():
    # zatca-008: the Basic-auth token must be the binarySecurityToken (certificate_pem) itself, not a
    # re-base64'd copy. cert_base64 is gone; certificate_pem is what the direct client uses.
    st = MemoryDataStore()
    ss = SettingsStore(st, "seal-key")
    run(ss.store_csid("t1", certificate_pem="BST-TOKEN-ALREADY-B64", secret="sec",
                      private_key_pem="key"))
    secrets = run(ss.load_secrets("t1"))
    assert secrets["certificate_pem"] == "BST-TOKEN-ALREADY-B64"
    assert "cert_base64" not in secrets

    # And the direct client's Authorization header decodes to exactly BST:secret (single layer).
    import base64
    from app.adapters.fatoora.direct import DirectFatoora
    h = DirectFatoora("https://gw.example.test")._headers(
        {"token": secrets["certificate_pem"], "secret": secrets["secret"]}, clearance=False)
    decoded = base64.b64decode(h["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "BST-TOKEN-ALREADY-B64:sec"


def test_egress_boot_guard_rejects_an_unallowlisted_gateway_host():
    # zatca-009: with an allowlist configured, a FATOORA_BASE_URL host outside it fails fast at boot.
    from app.config import Settings
    from app.container import _guard_egress_hosts, ConfigError
    ok = Settings(EGRESS_ALLOWED_HOSTS="gw.zatca.gov.sa", FATOORA_BASE_URL="https://gw.zatca.gov.sa/e")
    _guard_egress_hosts(ok)                                   # allow-listed → no raise
    bad = Settings(EGRESS_ALLOWED_HOSTS="gw.zatca.gov.sa", FATOORA_BASE_URL="https://evil.example/e")
    with pytest.raises(ConfigError):
        _guard_egress_hosts(bad)
    # No allowlist configured → URLs are trusted config, no enforcement.
    _guard_egress_hosts(Settings(EGRESS_ALLOWED_HOSTS="", FATOORA_BASE_URL="https://anything.test/e"))


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
    # A present blob under the WRONG key is a loud fault now, not a silent None (zatca-007).
    from app.core.seal import CsidSealError
    with pytest.raises(CsidSealError):
        unseal(tok, "wrong")


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
    assert inv2.icv == 2                                             # allocation head advanced
    head = run(st.get("zatca_index", "head::t1"))
    assert head["reserved_icv"] == 2                                 # reservation hint advanced
    # the signed-chain head (authoritative PIH source, zatca-01) advances as invoices sign
    signed = run(st.get("zatca_index", "signed::t1"))
    assert signed["signed_icv"] == 2 and signed["last_hash"]
    assert run(st.get("zatca_index", "num::t1::INV-1")) is not None


def test_concurrent_allocate_slot_never_duplicates_an_icv():
    # The ICV chain must stay unique under concurrency — two finalizes racing for one tenant used to
    # both mint the same ICV (corrupting the PIH/ICV chain). The atomic icv-claim prevents it.
    import asyncio as _a
    st = MemoryDataStore()
    store = InvoiceStore(st)

    async def _race():
        return await _a.gather(*[
            store.allocate_slot("t1", f"INV-{i}", "standard") for i in range(20)])

    results = run(_race())
    icvs = sorted(inv.icv for inv, created in results if created)
    assert icvs == list(range(1, 21))                      # 20 distinct ICVs, 1..20, no duplicate


def test_tenant_isolation():
    st = MemoryDataStore()
    s = SettingsStore(st, "k" * 8)
    for t in ("t1", "t2"):
        run(s.store_csid(t, certificate_pem="C", secret="S", private_key_pem="K"))
    z = _svc(st, MockSigner(), MockFatoora())
    run(z.finalize(INV, "t1"))
    assert run(z.verify_chain("t2"))["count"] == 0
