"""credit() writes the grant BEFORE the atomic balance credit and rolls it back on failure.

The old order (credit the balance, THEN put the grant) had a window: a failed grant write left the
balance already credited but the idempotency claim stuck `pending` — so once the claim lease lapsed,
a redelivered settlement webhook re-ran credit() and DOUBLE-credited. Grant-first + rollback closes
it: a grant failure credits nothing, so a retry credits exactly once.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

FUNDS = (("cash", 1, True, True),)
CFG = {"funds": FUNDS, "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8, "idempotency_claim_lease_seconds": 60}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t


class FlakyGrantStore(MemoryDataStore):
    """A MemoryDataStore that fails the FIRST write to the grants collection, then behaves normally."""
    def __init__(self):
        super().__init__()
        self.fail_grants = True

    async def put(self, collection, key, value):
        if collection == "grants" and self.fail_grants:
            self.fail_grants = False
            raise RuntimeError("simulated grant-store failure")
        return await super().put(collection, key, value)


def _bal(svc, t="t1"):
    return run(svc.balance(t))["funds"].get("cash", 0)


def test_a_failed_grant_write_credits_nothing_then_a_retry_credits_once():
    store = FlakyGrantStore()
    clock = Clock()
    svc = WalletService(store=store, clock=clock, config=CFG)

    # A credit that carries an expiry (so a grant row is written). The first attempt fails on the
    # grant write — and because the grant is written BEFORE the balance mutate, NOTHING is credited.
    with pytest.raises(RuntimeError):
        run(svc.credit(tenant="t1", fund="cash", amount=1000, source="settle-1",
                       expires_at=clock.now() + 86400))
    assert _bal(svc) == 0                                  # no orphan credit

    # The settlement webhook is redelivered after the claim lease lapses. The store is healthy now,
    # so it credits — exactly ONCE, not twice.
    clock.t += 120                                         # past the 60s claim lease
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="settle-1",
                   expires_at=clock.now() + 86400))
    assert _bal(svc) == 1000                               # credited once, not 2000


def test_a_normal_credit_still_writes_its_grant():
    store = FlakyGrantStore(); store.fail_grants = False
    clock = Clock()
    svc = WalletService(store=store, clock=clock, config=CFG)
    res = run(svc.credit(tenant="t1", fund="cash", amount=500, source="c1",
                         expires_at=clock.now() + 86400))
    assert res["credited"] == 500 and res.get("grant_id")
    assert _bal(svc) == 500
