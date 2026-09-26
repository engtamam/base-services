"""Audit FIN-011 — the zero / negative amount edges of credit / spend / reserve.

A zero amount is permitted (it is non-negative) and must be a clean no-op on the balance; a NEGATIVE
amount must be refused, never silently debit via a credit or mint via a spend.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.money import MoneyError
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


def _svc():
    return WalletService(store=MemoryDataStore(), clock=Clock(), config={**CFG})


def _cash(svc):
    return run(svc.balance("t1"))["funds"].get("cash", 0)


def test_a_zero_credit_is_a_clean_noop_no_floor_row():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=0, source="z0"))
    assert _cash(svc) == 0
    # the permanent (non-forfeitable) floor must not gain a zero entry
    w = run(svc._wallet("t1"))
    assert w.permanent.get("cash", 0) == 0


def test_a_negative_credit_is_refused():
    svc = _svc()
    with pytest.raises(MoneyError):
        run(svc.credit(tenant="t1", fund="cash", amount=-100, source="neg"))
    assert _cash(svc) == 0


def test_a_zero_spend_is_a_noop_success():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="seed"))
    run(svc.spend(tenant="t1", net=0, source="s0"))
    assert _cash(svc) == 1000                      # nothing debited


def test_a_negative_spend_is_refused():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="seed"))
    with pytest.raises(MoneyError):
        run(svc.spend(tenant="t1", net=-50, source="sneg"))
    assert _cash(svc) == 1000


def test_a_zero_reserve_holds_nothing():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="seed"))
    run(svc.reserve(tenant="t1", net=0, source="r0"))
    bal = run(svc.balance("t1"))
    assert bal["funds"].get("cash", 0) == 1000
    assert bal.get("held", {}).get("cash", 0) == 0   # no reservation counted
