"""Payout: money leaving the platform. The properties that matter because it moves real money —
only withdrawable funds may leave, a locked (promo) fund never does, the pool is never overdrawn,
a request is idempotent, and a FAILED transfer returns the money exactly."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import funds as F
from app.core.wallet_service import WalletError, WalletService

# cash is withdrawable, promo is locked (spend-only).
CFG = {"funds": (("promo", 1, False, False), ("cash", 2, True, True)), "vat_bp": 1500,
       "currency": "SAR", "hold_ttl": 900, "hold_sweep_limit": 500, "grant_sweep_limit": 500,
       "idempotency_ttl": 3600, "ledger_list_limit": 100, "cas_max_retries": 8, "withdraw_min": 0}


def run(c):
    return asyncio.run(c)


class Clock:
    t = 1000.0

    def now(self) -> float:
        return self.t


def _svc(st, **over):
    return WalletService(store=st, clock=Clock(), config={**CFG, **over})


def test_withdraw_debits_only_withdrawable_funds_never_the_locked_one():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="promo", amount=5000, source="c1"))   # locked
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c2"))   # withdrawable
    res = run(w.withdraw(tenant="m", amount=7000, source="wd1"))
    assert res["state"] == "pending" and res["applied"] == {"cash": 7000}
    bal = run(w.balance("m"))["funds"]
    assert bal["cash"] == 3000 and bal["promo"] == 5000                 # promo untouched


def test_withdraw_never_exceeds_the_withdrawable_pool_even_with_a_large_locked_balance():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="promo", amount=100000, source="c1"))
    run(w.credit(tenant="m", fund="cash", amount=5000, source="c2"))
    with pytest.raises(F.InsufficientFunds):
        run(w.withdraw(tenant="m", amount=6000, source="wd1"))          # only 5000 is withdrawable


def test_a_platform_with_no_withdrawable_fund_refuses():
    st = MemoryDataStore()
    w = _svc(st, funds=(("promo", 1, False, False),))
    run(w.credit(tenant="m", fund="promo", amount=5000, source="c"))
    with pytest.raises(WalletError):
        run(w.withdraw(tenant="m", amount=1000, source="wd"))


def test_withdraw_is_idempotent_by_source():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c"))
    a = run(w.withdraw(tenant="m", amount=3000, source="wd1"))
    b = run(w.withdraw(tenant="m", amount=3000, source="wd1"))
    assert b.get("idempotent") is True and b["withdrawal_id"] == a["withdrawal_id"]
    assert run(w.balance("m"))["funds"]["cash"] == 7000                 # debited once, not twice


def test_settle_paid_keeps_the_debit():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c"))
    wd = run(w.withdraw(tenant="m", amount=4000, source="wd1"))
    assert run(w.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=True))["state"] == "paid"
    assert run(w.balance("m"))["funds"]["cash"] == 6000


def test_settle_failed_returns_the_money_and_stays_reconciled():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c"))
    wd = run(w.withdraw(tenant="m", amount=4000, source="wd1"))
    assert run(w.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=False))["state"] == "failed"
    assert run(w.balance("m"))["funds"]["cash"] == 10000               # a failed transfer costs nothing
    assert run(w.reconcile("m"))["consistent"] is True                 # ledger still equals the balance


def test_settle_is_idempotent_no_double_move():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c"))
    wd = run(w.withdraw(tenant="m", amount=4000, source="wd1"))
    run(w.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=False))
    again = run(w.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=True))   # late "paid" retry
    assert again.get("idempotent") is True and again["state"] == "failed"
    assert run(w.balance("m"))["funds"]["cash"] == 10000


def test_below_minimum_is_refused():
    st = MemoryDataStore(); w = _svc(st, withdraw_min=5000)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c"))
    with pytest.raises(WalletError):
        run(w.withdraw(tenant="m", amount=1000, source="wd"))


# ── wallet-001: a withdrawal is written UNCONFIRMED first, PENDING only after the debit ──────────────
def test_withdraw_row_is_pending_only_after_the_debit_lands():
    st = MemoryDataStore(); w = _svc(st)
    run(w.credit(tenant="m", fund="cash", amount=10000, source="c1"))
    res = run(w.withdraw(tenant="m", amount=4000, source="wd1"))
    # the returned + persisted state is PENDING (debit committed), never UNCONFIRMED
    row = run(st.get("withdrawals", res["withdrawal_id"]))
    assert res["state"] == "pending" and row["state"] == "pending"


def test_settle_ignores_an_unconfirmed_withdrawal():
    # A crash between the row write and the debit leaves an UNCONFIRMED row (never debited). The payout
    # settle path must NOT act on it — only PENDING (debited) withdrawals are payable/reversible.
    from app.core.models import WITHDRAW_UNCONFIRMED, Withdrawal
    st = MemoryDataStore(); w = _svc(st)
    wd = Withdrawal(id="wd_orphan", tenant="m", amount=4000, applied={"cash": 4000},
                    destination="", state=WITHDRAW_UNCONFIRMED, source="wd1", reason="",
                    created=1000.0)
    run(st.put("withdrawals", wd.id, wd.to_dict()))
    out = run(w.settle_withdrawal(withdrawal_id="wd_orphan", paid=False))
    # not PENDING → treated as already-settled/no-op; no reversal credit invented for an un-debited row
    assert out.get("idempotent") is True and out["state"] == WITHDRAW_UNCONFIRMED
    assert run(w.balance("m"))["funds"].get("cash", 0) == 0     # nothing credited back
