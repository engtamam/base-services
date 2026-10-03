"""Non-security MEDIUM remediation (zatca-service):
  011  verify_chain reports unsigned_link at a mid-chain 'failed'/'submitted' row, not pih_mismatch after it.
  016  classify() flags a 401 as auth_error (distinct, diagnosable) while keeping it retryable.
  017  a re-finalize of an ACCEPTED number with a DIFFERENT payload is a conflict, not a silent replay.
  021  a malformed vat_rate is a 422 that burns no ICV.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.qr.null import NullQr
from app.core.chain import verify_chain
from app.core.classify import classify
from app.core.stores import InvoiceStore, SettingsStore
from app.core.zatca_service import ZatcaConflict, ZatcaInputError, ZatcaService

RETRY = {"base_ms": 60_000, "max_ms": 21_600_000, "max_attempts": 12}
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def run(c):
    return asyncio.run(c)


class _Signer:
    driver = "mock"
    def sign(self, **k):
        return {"signedXml": "<s/>", "invoiceHash": "HASH1", "qrBase64": "Q",
                "signatureBase64": "G", "signingTimeUtc": "T"}


class _Fatoora:
    driver = "mock"
    def report(self, **k): return 200, {"reportingStatus": "REPORTED"}
    def clear(self, **k): return 200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<stamped/>"}


def _svc(store):
    return ZatcaService(invoices=InvoiceStore(store), settings=SettingsStore(store, "k" * 8),
                        signer=_Signer(), fatoora=_Fatoora(), qr=NullQr(), vat_rate="0.15",
                        currency="SAR", retry=RETRY)


# ── 011: a mid-chain non-finalized row is unsigned_link at its own ICV ─────────
def test_mid_chain_submitted_row_reports_unsigned_link():
    links = [
        {"icv": 1, "pih": "GEN", "hash": "H1", "status": "signed"},
        {"icv": 2, "pih": "H1", "hash": "", "status": "submitted"},   # not finalized, mid-chain
        {"icv": 3, "pih": "H2", "hash": "H3", "status": "signed"},
    ]
    out = verify_chain(links, genesis_pih="GEN")
    assert out["ok"] is False
    assert out["break_at"] == 2 and out["reason"] == "unsigned_link"


# ── 016: 401 is a distinct auth error, still retryable ─────────────────────────
def test_401_is_auth_error_and_retryable():
    out = classify(401, {}, success_status="reported")
    assert out["outcome"] == "pending" and out["auth_error"] is True
    assert "CSID" in out["message"] or "credential" in out["message"]


def test_500_is_a_plain_transient_not_auth():
    out = classify(500, {}, success_status="reported")
    assert out["outcome"] == "pending" and out["auth_error"] is False


# ── 017: re-finalize with a changed payload is a conflict ──────────────────────
def test_refinalize_with_changed_payload_conflicts():
    from app.core.zatca_service import _payload_fingerprint
    store = MemoryDataStore()
    z = _svc(store)
    # Seed an ALREADY-ACCEPTED invoice for (t1, INV-1) whose draft recorded the first payload's fingerprint.
    fp = _payload_fingerprint(INV)
    run(z._inv.allocate_slot("t1", "INV-1", "standard"))
    inv = run(z._inv.by_number("t1", "INV-1"))
    inv.status = "reported"
    inv.draft = {"unsigned_xml": "<x/>", "payload_fp": fp}
    run(z._inv.save(inv))

    # An identical replay is idempotent (no raise)...
    out = run(z.finalize(INV, "t1"))
    assert out["status"] == "reported"
    # ...but a DIFFERENT payload for the same accepted number is a conflict.
    changed = {**INV, "lines": [{"description": "Plan", "qty": 1, "amount_minor": 99_999}]}
    with pytest.raises(ZatcaConflict):
        run(z.finalize(changed, "t1"))


# ── 021: a malformed vat_rate is a 422 that burns no ICV ───────────────────────
@pytest.mark.parametrize("bad", ["abc", "15%", "", "-0.1"])
def test_malformed_vat_rate_is_rejected_and_burns_no_icv(bad):
    store = MemoryDataStore()
    z = _svc(store)
    with pytest.raises(ZatcaInputError):
        run(z.finalize({**INV, "vat_rate": bad}, "t1"))
    # no slot allocated → no head reserved
    head = run(store.get("zatca_index", "head::t1"))
    assert head is None or int(head.get("reserved_icv", 0)) == 0
