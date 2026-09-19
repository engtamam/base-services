"""wallet-r2-03 (crash window): the terminal-state write must land BEFORE the money `_mutate`, so a
crash STRICTLY BETWEEN the money move and the state write can never double-move money on a replay.

Each test drives a once-only money op, but injects a crash on the terminal-state `put` (the source
object: withdrawal / hold / grant). It then advances the clock past the pending-claim lease so the op
is re-claimable, and REPLAYS it.

  * PRE-FIX ordering was `_mutate` → state `put`: the injected crash fires AFTER the money moved but
    BEFORE the state landed, so the replay re-reads the non-terminal state and re-moves money
    (double-reverse / double-charge / lost-reclaimable-balance).
  * FIXED ordering is state `put` → `_mutate`: the crash fires BEFORE any money moves, so the replay
    finds the op un-started and moves money EXACTLY ONCE. A hard crash can only ever leave money
    un-moved, never moved twice.

Money is integer minor units throughout.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

DAY = 86400
TTL = 3600
LEASE = 60
FUNDS = (("cash", 1, True, True),)
CFG = {"funds": FUNDS, "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": TTL,
       "ledger_list_limit": 100, "cas_max_retries": 8, "idempotency_claim_lease_seconds": LEASE}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


def _svc(clock, policies=None):
    cfg = {**CFG}
    if policies is not None:
        cfg["fund_expiry_policies"] = policies
    return WalletService(store=MemoryDataStore(), clock=clock, config=cfg)


def _bal(svc, t="t1"):
    return run(svc.balance(t))["funds"].get("cash", 0)


def _past_lease(clk):
    clk.t += LEASE + 10        # the pending claim's short lease has lapsed → re-claimable


def _crash_once_on_put(svc, collection):
    """Make the NEXT `put` to `collection` raise (a simulated process crash at that persistence),
    then behave normally. This is exactly the seam that moved between pre- and post-fix code."""
    real = svc._store.put
    state = {"armed": True}

    async def patched(coll, key, value, *a, **k):
        if state["armed"] and coll == collection:
            state["armed"] = False
            raise RuntimeError("simulated crash persisting terminal state")
        return await real(coll, key, value, *a, **k)

    svc._store.put = patched
    return state


# ── settle_withdrawal (paid=False reversal) ───────────────────────────────────────
def test_settle_reversal_crash_then_replay_reverses_once():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    wd = run(svc.withdraw(tenant="t1", amount=400, destination="iban", source="w1"))
    assert _bal(svc) == 600
    _crash_once_on_put(svc, "withdrawals")
    try:
        run(svc.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=False, source="cb1"))
    except RuntimeError:
        pass                                    # crash between the money move and the state write
    _past_lease(clk)
    run(svc.settle_withdrawal(withdrawal_id=wd["withdrawal_id"], paid=False, source="cb1"))
    assert _bal(svc) == 1000                     # reversed EXACTLY once. pre-fix: 1400 (double reverse)


# ── confirm (hold → spend) ────────────────────────────────────────────────────────
def test_confirm_crash_then_replay_charges_once():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    hold = run(svc.reserve(tenant="t1", net=300, source="r1"))
    _crash_once_on_put(svc, "holds")
    try:
        run(svc.confirm(hold_id=hold["hold_id"]))
    except RuntimeError:
        pass                                    # crash between the debit and the state write
    _past_lease(clk)                            # < hold_ttl (900), so the hold is still ACTIVE
    run(svc.confirm(hold_id=hold["hold_id"]))
    assert _bal(svc) == 700                      # charged EXACTLY once (300). pre-fix: 400 (double)


# ── reclaim (dormant grant → balance) ─────────────────────────────────────────────
def test_reclaim_crash_then_replay_credits_once():
    clk = Clock()
    svc = _svc(clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    gid = next(iter(svc._store._data["grants"]))
    clk.t += 31 * DAY
    run(svc.sweep_grants())                      # -> dormant, swept=1000, funds=0
    assert _bal(svc) == 0
    _crash_once_on_put(svc, "grants")
    try:
        run(svc.reclaim(grant_id=gid, source="job1"))
    except RuntimeError:
        pass                                    # crash between the credit and the state write
    _past_lease(clk)
    run(svc.reclaim(grant_id=gid, source="job1"))
    assert _bal(svc) == 1000                     # reclaimed EXACTLY once. pre-fix: 2000 (double mint)


# ── sweep_grants (expiry clawback) ────────────────────────────────────────────────
def test_sweep_crash_then_replay_preserves_reclaimable_balance():
    """A crash while persisting the swept grant must not corrupt `swept`. Pre-fix, the clawback debit
    ran first (funds->0) then the state write crashed; the re-sweep recomputed spare=0 and stamped
    swept=0, permanently LOSING the customer's reclaimable dormant balance. Fixed: state (swept=1000)
    is stamped before the debit, so the re-sweep claws the right amount and reclaim restores it."""
    clk = Clock()
    svc = _svc(clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    gid = next(iter(svc._store._data["grants"]))
    clk.t += 31 * DAY
    _crash_once_on_put(svc, "grants")
    try:
        run(svc.sweep_grants())
    except RuntimeError:
        pass                                    # crash between the clawback and the state write
    _past_lease(clk)
    run(svc.sweep_grants())                      # re-sweep must claw the FULL 1000 and record swept=1000
    run(svc.reclaim(grant_id=gid, source="job1"))
    assert _bal(svc) == 1000                     # reclaimable balance preserved. pre-fix: 0 (lost)
