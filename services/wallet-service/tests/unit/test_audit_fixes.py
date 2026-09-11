"""Regression tests for the 2026-09-12 fleet-audit findings on wallet-service.

Each test would FAIL on the pre-fix code:
  01  idempotency version carried forward on finalize (memory proxy for the Postgres re-claim bug).
  03  reconcile() is bounded on the request path (reconcile_max_entries).
  04  withdraw() refuses an empty source — money leaving must be idempotent.
  05  confirm()/release() results carry the tenant (so the audit row is attributable).
  06  credit input validation happens before any critical side effect (validate_credit).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletError, WalletService, _IDEMPO

FUNDS = (("promo", 1, False), ("cash", 2, True, True))   # cash is withdrawable
CFG = {"funds": FUNDS, "vat_bp": 1500, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8, "withdraw_min": 0,
       "reconcile_max_entries": 5}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def _svc(store=None, clock=None):
    return WalletService(store=store or MemoryDataStore(), clock=clock or Clock(), config=CFG)


# ── 01: idempotency row keeps a non-zero version after finalize ────────────────
def test_finalized_idempotency_row_carries_a_version():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=100, source="s1"))
    row = run(st.get(_IDEMPO, "t|credit|s1"))
    assert row is not None and "result" in row
    # The finished row MUST retain a version (dropping it wedges a Postgres re-claim after TTL at 409).
    assert int(row.get("version", 0)) >= 1


# ── 03: reconcile is bounded on the request path ──────────────────────────────
def test_reconcile_refuses_an_oversized_ledger():
    st = MemoryDataStore(); w = _svc(st)
    for i in range(6):                                   # cap is 5
        run(w.credit(tenant="t", fund="cash", amount=1, source=f"c{i}"))
    res = run(w.reconcile("t"))
    assert res["consistent"] is None and res["reason"] == "LEDGER_TOO_LARGE"
    assert res["entries"] == 6 and res["max"] == 5


def test_reconcile_still_verifies_within_the_cap():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="t", fund="cash", amount=100, source="a"))
    res = run(w.reconcile("t"))
    assert res["consistent"] is True and res["stored"] == {"cash": 100}


# ── 04: withdraw requires a source (money leaving must be idempotent) ──────────
def test_withdraw_refuses_empty_source():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="seed"))
    with pytest.raises(WalletError) as e:
        run(w.withdraw(tenant="t", amount=100, destination="acct", source=""))
    assert "WITHDRAW_SOURCE_REQUIRED" in str(e.value)


def test_withdraw_with_source_is_idempotent():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="seed"))
    r1 = run(w.withdraw(tenant="t", amount=100, destination="acct", source="wd-1"))
    r2 = run(w.withdraw(tenant="t", amount=100, destination="acct", source="wd-1"))
    assert r2.get("idempotent") is True
    # only ONE payout: available cash dropped by 100, not 200
    bal = run(w.balance("t"))
    assert bal["funds"]["cash"] == 900


# ── 05: confirm/release results carry the tenant ──────────────────────────────
def test_confirm_and_release_results_carry_tenant():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="seed"))
    h1 = run(w.reserve(tenant="t", net=100, source="r1"))
    conf = run(w.confirm(hold_id=h1["entry"]["ref"] if "entry" in h1 else h1["hold_id"]))
    assert conf["tenant"] == "t"
    h2 = run(w.reserve(tenant="t", net=100, source="r2"))
    rel = run(w.release(hold_id=h2["hold_id"]))
    assert rel["tenant"] == "t"


# ── 06: credit validates inputs before side effects ───────────────────────────
def test_validate_credit_rejects_unknown_fund_and_negative():
    w = _svc()
    with pytest.raises(WalletError):
        w.validate_credit(fund="nope", amount=100)
    with pytest.raises(Exception):
        w.validate_credit(fund="cash", amount=-1)
    # and credit() itself still rejects before mutating
    with pytest.raises(WalletError):
        run(w.credit(tenant="t", fund="nope", amount=100, source="x"))
