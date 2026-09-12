"""Stuck-invoice recovery (zatca-r2-03 + r2-07) — each test FAILS on the pre-fix code.

Covers:
  orphan reconcile      a crash-orphaned icv:: claim is reclaimed and the number reused (r2-07)
  release refused       release-tail refuses when a HIGHER ICV is reserved (mid-chain, r2-03)
  release + reuse       release-tail frees the highest reserved unsigned ICV; the number is reused
  correct-and-resign    rebuilds a stuck unsigned ICV with the SAME number; the chain verifies
  head-lag              the reserved-vs-signed head gap surfaces past the stuck threshold
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.qr.null import NullQr
from app.core.stores import InvoiceStore, SettingsStore
from app.core.zatca_service import ZatcaService

RETRY = {"base_ms": 60_000, "max_ms": 21_600_000, "max_attempts": 12}
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def run(c):
    return asyncio.run(c)


class DistinctSigner:
    driver = "mock"

    def __init__(self):
        self._c = itertools.count(1)

    def sign(self, **k):
        n = next(self._c)
        return {"signedXml": f"<s{n}/>", "invoiceHash": f"HASH{n}", "qrBase64": "Q",
                "signatureBase64": "G", "signingTimeUtc": "T"}


class Fatoora:
    driver = "mock"

    def report(self, **k):
        return 200, {"reportingStatus": "REPORTED"}

    def clear(self, **k):
        return 200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<stamped/>"}


def _svc(store, *, signer=None, stuck_tail_threshold=5):
    return ZatcaService(invoices=InvoiceStore(store), settings=SettingsStore(store, "k" * 8),
                        signer=signer or DistinctSigner(), fatoora=Fatoora(), qr=NullQr(),
                        vat_rate="0.15", currency="SAR", retry=RETRY,
                        stuck_tail_threshold=stuck_tail_threshold)


def _onboard(store, *tenants):
    s = SettingsStore(store, "k" * 8)
    for t in tenants:
        run(s.store_csid(t, certificate_pem="C", secret="S", private_key_pem="K"))


# ── orphan reconcile: a crash-orphaned icv:: claim is reclaimed and the number reused ──────────
def test_orphan_icv_claim_is_reclaimed_and_number_reused():
    st = MemoryDataStore()
    store = InvoiceStore(st)
    # ICV 1 finalized normally (invoice row present, head reserved=1).
    run(store.allocate_slot("t1", "INV-1", "simplified"))
    # Simulate a crash mid-allocate of INV-2: the num:: and icv:: claims are written, but the
    # process died BEFORE the invoice row was saved and BEFORE the head hint advanced (its last step).
    run(st.create("zatca_index", "num::t1::INV-2", {"id": "orphan-id"}))
    run(st.create("zatca_index", "icv::t1::2", {"number": "INV-2"}))
    assert run(st.get("zatca_index", "icv::t1::2")) is not None
    assert run(store._invoice_at_icv("t1", 2)) is None       # no invoice behind the claim

    res = run(store.reconcile_orphans("t1"))
    assert res["reclaimed"] == [2] and res["count"] == 1
    assert run(st.get("zatca_index", "icv::t1::2")) is None   # claim freed
    assert run(st.get("zatca_index", "num::t1::INV-2")) is None

    # The next finalize REUSES ICV 2 — no permanent gap.
    inv2, created = run(store.allocate_slot("t1", "INV-2", "simplified"))
    assert created and inv2.icv == 2
    assert {r["number"]: r["icv"] for r in run(store._rows("t1"))} == {"INV-1": 1, "INV-2": 2}


def test_reconcile_never_touches_a_claim_with_a_real_invoice():
    st = MemoryDataStore()
    store = InvoiceStore(st)
    run(store.allocate_slot("t1", "INV-1", "simplified"))
    run(store.allocate_slot("t1", "INV-2", "simplified"))
    res = run(store.reconcile_orphans("t1"))
    assert res["count"] == 0                                  # both ICVs have invoices — untouched
    assert run(st.get("zatca_index", "icv::t1::1")) is not None
    assert run(st.get("zatca_index", "icv::t1::2")) is not None


# ── release refused when a higher ICV exists (mid-chain cannot be skipped) ─────────────────────
def test_release_tail_refused_when_higher_icv_exists():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    inv1, _ = run(z._inv.allocate_slot("t1", "INV-1", "simplified"))
    run(z._inv.allocate_slot("t1", "INV-2", "simplified"))    # a higher ICV now chains after inv1
    res = run(z.release_tail(inv1.id))
    assert res["ok"] is False and res["reason"] == "HIGHER_ICV_EXISTS"
    assert res["highest_reserved"] == 2
    # nothing was deleted — inv1 and its claims survive
    assert run(z._inv.get(inv1.id)) is not None
    assert run(st.get("zatca_index", "icv::t1::1")) is not None


def test_release_tail_refused_for_a_signed_invoice():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    run(z.finalize(INV, "t1"))                                # ICV 1 signs + reports
    inv = run(z._inv.by_number("t1", "INV-1"))
    assert inv.signed_xml
    res = run(z.release_tail(inv.id))
    assert res["ok"] is False and res["reason"] == "ALREADY_SIGNED"
    assert run(z._inv.get(inv.id)) is not None                # untouched


# ── release + reuse when it IS the tail ────────────────────────────────────────────────────────
def test_release_tail_frees_the_highest_unsigned_icv_and_number_is_reused():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    inv1, _ = run(z._inv.allocate_slot("t1", "INV-1", "simplified"))   # unsigned tail, ICV 1
    res = run(z.release_tail(inv1.id))
    assert res["ok"] and res["released_icv"] == 1 and res["number"] == "INV-1"
    assert run(z._inv.get(inv1.id)) is None
    assert run(st.get("zatca_index", "icv::t1::1")) is None
    assert run(st.get("zatca_index", "num::t1::INV-1")) is None
    assert run(z._inv.reserved_head("t1")) == 0               # head rolled back
    # a fresh invoice REUSES ICV 1 — the sequence stays gapless from 1
    inv2, created = run(z._inv.allocate_slot("t1", "INV-2", "simplified"))
    assert created and inv2.icv == 1
    assert run(z.verify_chain("t1"))["count"] in (0, 1)       # no gap regardless of sign state


# ── correct-and-resign fills the SAME number and the chain verifies ────────────────────────────
def test_correct_and_resign_fills_same_icv_and_chain_verifies():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    # A stuck, unsigned reserved ICV 1 (allocated but never signed).
    inv1, _ = run(z._inv.allocate_slot("t1", "INV-1", "simplified"))
    assert not inv1.signed_xml and inv1.icv == 1
    corrected = {**INV, "lines": [{"description": "Fixed", "qty": 2, "amount_minor": 5_000}]}
    res = run(z.correct_and_resign(inv1.id, corrected))
    assert res["ok"] and res["status"] == "reported"
    filled = run(z._inv.by_number("t1", "INV-1"))
    assert filled.icv == 1 and filled.signed_xml              # SAME ICV, now signed
    assert filled.id == inv1.id                               # same slot, not a new row
    v = run(z.verify_chain("t1"))
    assert v["ok"] and v["count"] == 1


def test_correct_and_resign_refuses_an_already_signed_invoice():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    run(z.finalize(INV, "t1"))
    inv = run(z._inv.by_number("t1", "INV-1"))
    signed_bytes = inv.signed_xml
    res = run(z.correct_and_resign(inv.id, {**INV, "lines": [{"amount_minor": 1}]}))
    assert res["ok"] is False and res["reason"] == "ALREADY_SIGNED"
    assert run(z._inv.by_number("t1", "INV-1")).signed_xml == signed_bytes   # bytes untouched


# ── head-lag surfaces a stuck tail ─────────────────────────────────────────────────────────────
def test_head_lag_surfaces_when_reserved_leads_signed():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st, stuck_tail_threshold=1)
    run(z.finalize(INV, "t1"))                                # sign ICV 1 → signed head = 1
    run(z._inv.allocate_slot("t1", "INV-2", "simplified"))    # reserve 2, 3 (never signed)
    run(z._inv.allocate_slot("t1", "INV-3", "simplified"))
    lag = run(z.head_lag("t1"))
    assert lag["reserved_icv"] == 3 and lag["signed_icv"] == 1
    assert lag["lag"] == 2
    assert lag["threshold"] == 1 and lag["stuck"] is True

    healthy = run(_svc(st, stuck_tail_threshold=5).head_lag("t1"))
    assert healthy["lag"] == 2 and healthy["stuck"] is False   # same gap, under threshold


# ── sweep reconciles orphans for tenants in the due set ────────────────────────────────────────
def test_sweep_reconciles_orphans_for_due_tenants():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st)
    # ICV 1 finalized + reported. Then a crash-orphaned ICV 2 claim (no invoice), and a real ICV 3
    # pending invoice that will DEFER forever behind the orphan — putting t1 in the due set.
    run(z.finalize(INV, "t1"))
    run(st.create("zatca_index", "num::t1::INV-2", {"id": "orphan"}))
    run(st.create("zatca_index", "icv::t1::2", {"number": "INV-2"}))
    # a real ICV 3 invoice (reserved head → 3), stuck behind the ICV-2 hole: finalize defers it
    # (awaiting ICV 2) and schedules a retry — force it DUE so it appears in the sweep's due set.
    run(z.finalize({**INV, "number": "INV-3"}, "t1"))
    inv3 = run(z._inv.by_number("t1", "INV-3"))
    assert inv3.icv == 3 and inv3.status == "pending"
    inv3.next_retry_at = 1.0                                  # in the past → due now
    run(z._inv.save(inv3))
    out = run(z.sweep())
    assert out["orphans_reclaimed"] == 1
    assert run(st.get("zatca_index", "icv::t1::2")) is None
