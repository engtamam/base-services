"""Round-2 audit regressions — each test FAILS on the pre-fix code.

Covers:
  r2-01  atomic number idempotency (concurrent same-number finalize mints ONE ICV)
  r2-02  concurrent / out-of-order sign never forks the chain or overwrites signed bytes
  r2-04  due_for_retry tolerates a swept set mixing None and float next_retry_at
  r2-05  qty / issued_at validated before a slot is burned
  r2-06  resubmit re-signs a sign-capped 'failed' invoice (never submits empty XML)
  r2-08  classify treats a body-less / partial 2xx as transient, not success
  r2-11  finalize returns a clean input error (not KeyError) on a missing number
"""
from __future__ import annotations

import asyncio
import itertools
import time as _time

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.qr.null import NullQr
from app.core.classify import classify
from app.core.stores import InvoiceStore, SettingsStore
from app.core.zatca_service import ZatcaInputError, ZatcaService
from app.ports.zatca_signer import SignerUnavailable

RETRY = {"base_ms": 60_000, "max_ms": 21_600_000, "max_attempts": 12}
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def run(c):
    return asyncio.run(c)


class InterleavingStore(MemoryDataStore):
    """A memory store that YIELDS on every get(), so concurrent coroutines interleave at read
    points — the deterministic way to expose a check-then-act race without real threads."""

    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


class DistinctSigner:
    driver = "mock"

    def __init__(self):
        self._c = itertools.count(1)          # count.__next__ is atomic under the GIL

    def sign(self, **k):
        n = next(self._c)                     # a DIFFERENT hash per call — a fork would be visible
        return {"signedXml": f"<s{n}/>", "invoiceHash": f"HASH{n}", "qrBase64": "Q",
                "signatureBase64": "G", "signingTimeUtc": "T"}


class CountingFatoora:
    driver = "mock"

    def __init__(self):
        self.reports = 0
        self.clears = 0

    def report(self, **k):
        self.reports += 1
        return 200, {"reportingStatus": "REPORTED"}

    def clear(self, **k):
        self.clears += 1
        return 200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<stamped/>"}


def _svc(store, signer, fatoora):
    return ZatcaService(invoices=InvoiceStore(store), settings=SettingsStore(store, "k" * 8),
                        signer=signer, fatoora=fatoora, qr=NullQr(), vat_rate="0.15",
                        currency="SAR", retry=RETRY)


def _onboard(store, *tenants):
    s = SettingsStore(store, "k" * 8)
    for t in tenants:
        run(s.store_csid(t, certificate_pem="C", secret="S", private_key_pem="K"))


# ── r2-01: atomic number idempotency ────────────────────────────────────────────
def test_concurrent_same_number_allocates_exactly_one_icv():
    st = InterleavingStore()
    store = InvoiceStore(st)

    async def _race():
        return await asyncio.gather(*[store.allocate_slot("t1", "INV-1", "standard")
                                      for _ in range(6)])

    results = run(_race())
    created = [inv for inv, was_created in results if was_created]
    ids = {inv.id for inv, _ in results}
    icvs = {inv.icv for inv, _ in results}
    assert len(created) == 1                    # exactly one caller minted a slot
    assert ids == {created[0].id}               # everyone got the SAME invoice
    assert icvs == {created[0].icv}             # ... and the SAME single ICV
    # and only ONE invoice row + ONE icv claim exist for the number
    rows = run(store._rows("t1"))
    assert len([r for r in rows if r["number"] == "INV-1"]) == 1
    assert run(st.get("zatca_index", f"icv::t1::{created[0].icv}")) is not None
    assert run(st.get("zatca_index", f"icv::t1::{created[0].icv + 1}")) is None


# ── orphaned num:: claim (crash before icv:: claim) self-heals, never a permanent 500 ──
def test_stale_orphan_num_claim_self_heals_and_reuses_the_number():
    """A crash between the num:: claim and save() (BEFORE any icv:: claim) leaves a num:: orphan that
    reconcile_orphans — which walks icv:: — never sees. A later allocate must free the STALE claim and
    mint the invoice, not spin and 500 forever."""
    st = MemoryDataStore()
    store = InvoiceStore(st)
    run(st.create("zatca_index", "num::t1::INV-9",
                  {"id": "phantom", "claimed_ts": _time.time() - 3600}))   # crash aftermath, no row
    inv, created = run(store.allocate_slot("t1", "INV-9", "standard"))
    assert created is True and inv.number == "INV-9" and inv.icv == 1
    assert run(st.get("zatca_index", "num::t1::INV-9"))["id"] == inv.id     # claim now points to it
    assert run(store.by_number("t1", "INV-9")).id == inv.id


def test_fresh_num_claim_is_not_stolen_from_an_inflight_winner():
    """A just-written num:: claim with no invoice yet (a live winner mid-allocate) is NOT reclaimed —
    only a claim older than the stale threshold is treated as a crash orphan."""
    st = MemoryDataStore()
    store = InvoiceStore(st)
    run(st.create("zatca_index", "num::t1::INV-7", {"id": "inflight", "claimed_ts": _time.time()}))
    with pytest.raises(RuntimeError):
        run(store.allocate_slot("t1", "INV-7", "standard"))
    assert run(st.get("zatca_index", "num::t1::INV-7"))["id"] == "inflight"   # untouched


# ── r2-02: concurrent sign never forks the chain / overwrites signed bytes ───────
def test_concurrent_finalize_same_invoice_no_fork_single_submit():
    st = InterleavingStore()
    _onboard(st, "t1")
    fat = CountingFatoora()
    z = _svc(st, DistinctSigner(), fat)

    async def _race():
        return await asyncio.gather(z.finalize(INV, "t1"), z.finalize(INV, "t1"))

    run(_race())
    inv = run(InvoiceStore(st).by_number("t1", "INV-1"))
    head = run(st.get("zatca_index", "signed::t1"))
    assert inv.icv == 1
    assert inv.signed_xml and inv.hash                       # a real signed body was persisted
    assert head["signed_icv"] == 1
    assert inv.hash == head["last_hash"]                     # stored body == chain head (NO fork)
    assert fat.reports == 1                                  # the loser did NOT also submit
    assert run(z.verify_chain("t1"))["ok"]


def test_out_of_order_sign_defers_until_predecessor_signed():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st, DistinctSigner(), CountingFatoora())
    # reserve ICV 1 and 2, but sign #2 first
    run(z._inv.allocate_slot("t1", "INV-1", "simplified"))
    inv2, _ = run(z._inv.allocate_slot("t1", "INV-2", "simplified"))
    # build a draft for #2 then attempt to sign it out of order
    r2_first = run(z.finalize({**INV, "number": "INV-2"}, "t1"))
    assert r2_first["status"] == "pending"                  # deferred — ICV 1 not signed yet
    assert run(st.get("zatca_index", "signed::t1")) is None
    run(z.finalize({**INV, "number": "INV-1"}, "t1"))       # now #1 signs → head at 1
    run(z.finalize({**INV, "number": "INV-2"}, "t1"))       # ... #2 can follow
    assert run(z.verify_chain("t1"))["ok"]
    head = run(st.get("zatca_index", "signed::t1"))
    assert head["signed_icv"] == 2


# ── r2-04: mixed None/float retry schedule must not crash the memory sweep ───────
def test_due_for_retry_tolerates_none_and_float_next_retry_at():
    st = MemoryDataStore()
    rows = [
        {"id": "a", "tenant": "t1", "number": "1", "icv": 1, "status": "pending",
         "next_retry_at": None, "created": "2024-01-01"},
        {"id": "b", "tenant": "t1", "number": "2", "icv": 2, "status": "signed",
         "next_retry_at": 500.0, "created": "2024-01-02"},
        {"id": "c", "tenant": "t1", "number": "3", "icv": 3, "status": "submitted",
         "next_retry_at": 2000.0, "created": "2024-01-03"},
    ]
    for r in rows:
        run(st.put("zatca_invoices", r["id"], r))
    due = run(InvoiceStore(st).due_for_retry(now_ts=1000.0))   # pre-fix: TypeError (str vs float)
    ids = [i.id for i in due]
    assert ids == ["b"]                         # the due one; None skipped, future not reached


# ── r2-05: build-coercible fields validated before a slot is burned ─────────────
@pytest.mark.parametrize("bad", [
    {**INV, "lines": [{"description": "x", "qty": "two", "amount_minor": 10_000}]},
    {**INV, "issued_at": "not-epoch"},
])
def test_bad_qty_or_issued_at_is_422_and_burns_no_icv(bad):
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st, DistinctSigner(), CountingFatoora())
    with pytest.raises(ZatcaInputError):
        run(z.finalize(bad, "t1"))
    assert run(st.get("zatca_index", "head::t1")) is None      # no ICV reserved
    assert run(st.get("zatca_index", "num::t1::INV-1")) is None
    assert run(z.verify_chain("t1"))["count"] == 0


# ── r2-06: resubmit recovers a sign-capped 'failed' invoice ─────────────────────
def test_resubmit_re_signs_a_failed_unsigned_invoice():
    class DownSigner(DistinctSigner):
        def sign(self, **k):
            raise SignerUnavailable("signer down")

    class EmptyRejectingFatoora(CountingFatoora):
        def report(self, *, signed_xml, **k):
            self.reports += 1
            if not signed_xml:                  # an empty document is a hard reject
                return 400, {"validationResults": {"errorMessages": [{"message": "EMPTY_XML"}]}}
            return 200, {"reportingStatus": "REPORTED"}

    st = MemoryDataStore()
    _onboard(st, "t1")
    # finalize with a down signer -> pending with a built draft, no signed_xml
    run(_svc(st, DownSigner(), EmptyRejectingFatoora()).finalize(INV, "t1"))
    inv = run(InvoiceStore(st).by_number("t1", "INV-1"))
    assert inv.draft and not inv.signed_xml
    inv.status = "failed"                        # simulate the sign-deferral cap (zatca-r2-03/06)
    inv.next_retry_at = None
    run(InvoiceStore(st).save(inv))
    # resubmit with a healthy signer must RE-SIGN, not submit an empty document
    fat = EmptyRejectingFatoora()
    z = _svc(st, DistinctSigner(), fat)
    res = run(z.resubmit(inv.id))
    assert res["ok"] and res["status"] == "reported"
    assert run(InvoiceStore(st).by_number("t1", "INV-1")).signed_xml


# ── r2-08: partial / body-less 2xx is transient, not silent success ─────────────
def test_classify_bodyless_and_partial_2xx_are_pending():
    # clearance: body-less 2xx and CLEARED-without-document are NOT terminal success
    assert classify(200, {}, success_status="cleared")["outcome"] == "pending"
    assert classify(200, {"clearanceStatus": "CLEARED"}, success_status="cleared")["outcome"] == "pending"
    # clearance: real success needs the stamped document
    ok = classify(200, {"clearanceStatus": "CLEARED", "clearedInvoice": "<x/>"}, success_status="cleared")
    assert ok["outcome"] == "cleared" and ok["cleared_xml"] == "<x/>"
    # reporting: body-less 2xx retries; NOT_* / errors stay hard rejects
    assert classify(200, {}, success_status="reported")["outcome"] == "pending"
    assert classify(200, {"reportingStatus": "NOT_REPORTED"}, success_status="reported")["outcome"] == "rejected"
    assert classify(200, {"reportingStatus": "REPORTED"}, success_status="reported")["outcome"] == "reported"


# ── r2-11: missing number is a clean input error, not a KeyError 500 ─────────────
def test_finalize_missing_number_is_input_error():
    st = MemoryDataStore()
    _onboard(st, "t1")
    z = _svc(st, DistinctSigner(), CountingFatoora())
    with pytest.raises(ZatcaInputError):
        run(z.finalize({k: v for k, v in INV.items() if k != "number"}, "t1"))
