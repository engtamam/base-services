"""Money-integrity guards from the security audit: a duplicated confirm / settle / refund moves money
ONCE, and a debit can never raid HELD (reserved) funds."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService, WalletError
from app.core import funds as _funds

FUNDS = (("cash", 1, True, True),)
CFG = {"funds": FUNDS, "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8}


def run(c):
    return asyncio.run(c)


class Clock:
    t = 1000.0

    def now(self):
        return self.t


def _svc():
    return WalletService(store=MemoryDataStore(), clock=Clock(), config=CFG)


def _bal(svc, t="t1"):
    return run(svc.balance(t))["funds"].get("cash", 0)


def test_duplicate_confirm_charges_once():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    h = run(svc.reserve(tenant="t1", net=400, source="r1"))["hold_id"]
    first = run(svc.confirm(hold_id=h))
    again = run(svc.confirm(hold_id=h))               # replay
    assert again.get("idempotent") is True
    assert _bal(svc) == 600                            # 1000 - 400, charged exactly once


def test_duplicate_refund_credits_once():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    run(svc.spend(tenant="t1", net=300, source="s1"))
    seq = next(e["seq"] for e in run(svc.ledger("t1")) if e["type"] == "spend")
    run(svc.refund(tenant="t1", seq=seq))              # no source → used to mint
    run(svc.refund(tenant="t1", seq=seq))              # repeat must NOT re-credit
    run(svc.refund(tenant="t1", seq=seq))
    assert _bal(svc) == 1000                            # 1000 - 300 + 300 (once), not 1000+300+300


def test_duplicate_failed_settle_reverses_once():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    wd = run(svc.withdraw(tenant="t1", amount=400, destination="iban", source="w1"))["withdrawal_id"]
    assert _bal(svc) == 600                             # debited pending
    run(svc.settle_withdrawal(withdrawal_id=wd, paid=False, source="cb1"))
    run(svc.settle_withdrawal(withdrawal_id=wd, paid=False, source="cb2"))   # duplicate callback
    assert _bal(svc) == 1000                            # reversed exactly once, not 1400


def test_a_debit_can_never_raid_held_funds():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    run(svc.reserve(tenant="t1", net=800, source="r1"))   # 800 held, 200 available
    with pytest.raises((WalletError, _funds.InsufficientFunds)):
        run(svc.spend(tenant="t1", net=500, source="s1"))  # would eat the hold
    assert _bal(svc) == 1000                            # nothing spent
