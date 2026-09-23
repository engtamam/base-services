"""Audit money-IN finding: credit() idempotency must be crash-safe in BOTH directions. The CREDIT
ledger row is written ATOMICALLY with the money inside _mutate, so it — not a pre-mutate marker — is
the authoritative "already credited?" truth.

  * Crash BEFORE the mutate  → neither ledger nor marker → a redelivery must CREDIT (never a dropped,
    paid credit; the old pre-mutate marker could persist without money and suppress the credit forever).
  * Crash AFTER the mutate    → the ledger row exists → a redelivery must REPLAY (never a double credit).
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

CFG = {"funds": (("cash", 1, True, True),), "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8, "idempotency_claim_lease_seconds": 60}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


def _svc(store=None, policies=None):
    cfg = {**CFG}
    if policies is not None:
        cfg["fund_expiry_policies"] = policies
    return WalletService(store=store or MemoryDataStore(), clock=Clock(), config=cfg)


def _cash(svc):
    return run(svc.balance("t1"))["funds"].get("cash", 0)


def test_crash_before_mutate_then_redelivery_still_credits():
    """A hard crash inside _mutate (money never moved) must leave NO durable trace that suppresses a
    retry — the paid-for credit must land on redelivery."""
    store = MemoryDataStore(); svc = _svc(store)
    orig = svc._mutate

    async def boom(*a, **k):
        raise RuntimeError("SIGKILL mid-mutate")

    svc._mutate = boom
    try:
        run(svc.credit(tenant="t1", fund="cash", amount=100, source="pay-1"))
    except RuntimeError:
        pass
    assert _cash(svc) == 0                         # nothing credited
    # a fresh process would re-claim after the lease lapses; simulate the redelivery on a new instance
    svc2 = WalletService(store=store, clock=Clock(2000.0), config={**CFG})
    out = run(svc2.credit(tenant="t1", fund="cash", amount=100, source="pay-1"))
    assert out["credited"] == 100 and not out.get("idempotent")   # the credit finally lands
    assert _cash(svc2) == 100


def test_crash_after_mutate_then_redelivery_replays_no_double():
    """The money moved (ledger written) but the process died before the seen-marker / finalize. A
    redelivery must find the ledger row and REPLAY — never credit a second time."""
    store = MemoryDataStore(); svc = _svc(store)
    orig_put = svc._store.put
    seen_key = "t1|pay-2"

    async def put_then_boom(coll, key, row):
        # let the money + ledger land, then die exactly when stamping the fast-path seen marker
        if coll.endswith("credit_seen") or key == seen_key:
            raise RuntimeError("SIGKILL after mutate, before marker")
        return await orig_put(coll, key, row)

    svc._store.put = put_then_boom
    try:
        run(svc.credit(tenant="t1", fund="cash", amount=100, source="pay-2"))
    except RuntimeError:
        pass
    svc._store.put = orig_put                        # the crashed process is gone; store recovers
    assert _cash(svc) == 100                        # money DID move
    # redelivery on a clean instance: must NOT double-credit
    svc2 = WalletService(store=store, clock=Clock(2000.0), config={**CFG})
    out = run(svc2.credit(tenant="t1", fund="cash", amount=100, source="pay-2"))
    assert out["credited"] == 100 and out.get("idempotent") is True
    assert _cash(svc2) == 100                        # still 100 — no double credit


def test_replay_via_ledger_after_marker_ttl_would_expire():
    """Even if the fast-path seen marker were gone (e.g. a TTL store), the ledger keeps the credit
    idempotent forever."""
    store = MemoryDataStore(); svc = _svc(store)
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="pay-3"))
    # wipe the fast-path marker to simulate its loss; the ledger remains
    run(store.delete("wallet_credit_seen", "t1|pay-3"))
    # redeliver far past the idempotency-claim TTL (3600s) so the claim no longer short-circuits —
    # only the durable ledger can keep it idempotent now
    svc2 = WalletService(store=store, clock=Clock(1000.0 + 100000), config={**CFG})
    out = run(svc2.credit(tenant="t1", fund="cash", amount=100, source="pay-3"))
    assert out.get("idempotent") is True
    assert _cash(svc2) == 100                         # ledger-authoritative: no double credit
