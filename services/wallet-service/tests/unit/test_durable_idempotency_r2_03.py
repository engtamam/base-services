"""Round-2 wallet-r2-03: once-only money ops must be DURABLY idempotent — a guard on the SOURCE
OBJECT, independent of the idempotency-key TTL.

Each test drives the op once, then advances the injected clock PAST `idempotency_ttl` (and past the
claim lease) so the TTL claim row has lapsed and is re-claimable, then REPLAYS the same source/seq.
On the pre-fix code the replay re-executes and re-moves money (double-credit / refund-mint /
double-payout). Fixed: the durable backstop (seen-source marker / refunded_ref on the spend row /
existing Withdrawal by source / durable grant.state) refuses the second move forever.

Money is integer minor units throughout.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

DAY = 86400
TTL = 3600
FUNDS = (("cash", 1, True, True),)
CFG = {"funds": FUNDS, "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": TTL,
       "ledger_list_limit": 100, "cas_max_retries": 8, "idempotency_claim_lease_seconds": 60}


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


def _past_ttl(clk):
    clk.t += TTL + 10          # the finalized idempotency result has now expired and is re-claimable


# ── credit / top-up ──────────────────────────────────────────────────────────────
def test_credit_replayed_after_ttl_does_not_double_credit():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="settle-1"))
    assert _bal(svc) == 1000
    _past_ttl(clk)
    again = run(svc.credit(tenant="t1", fund="cash", amount=1000, source="settle-1"))  # redelivered
    assert again.get("idempotent") is True
    assert _bal(svc) == 1000            # pre-fix: 2000 (the claim TTL lapsed, so the credit re-ran)


def test_credit_with_grant_replayed_after_ttl_credits_once():
    clk = Clock()
    svc = _svc(clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=500, source="settle-2"))
    _past_ttl(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=500, source="settle-2"))
    assert _bal(svc) == 500
    assert len(svc._store._data.get("grants", {})) == 1     # no second grant minted either


# ── spend ────────────────────────────────────────────────────────────────────────
def test_spend_replayed_after_ttl_does_not_double_charge():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    run(svc.spend(tenant="t1", net=300, source="s1"))
    assert _bal(svc) == 700
    _past_ttl(clk)
    again = run(svc.spend(tenant="t1", net=300, source="s1"))   # replayed after TTL
    assert again.get("idempotent") is True
    assert _bal(svc) == 700            # pre-fix: 400 (the claim TTL lapsed, so the debit re-ran)


# ── refund ───────────────────────────────────────────────────────────────────────
def test_refund_replayed_after_ttl_does_not_mint():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    run(svc.spend(tenant="t1", net=300, source="s1"))
    seq = next(e["seq"] for e in run(svc.ledger("t1")) if e["type"] == "spend")
    run(svc.refund(tenant="t1", seq=seq))
    assert _bal(svc) == 1000            # 1000 - 300 + 300
    _past_ttl(clk)
    again = run(svc.refund(tenant="t1", seq=seq))           # replayed chargeback, TTL lapsed
    assert again.get("idempotent") is True
    assert _bal(svc) == 1000            # pre-fix: 1300 (a second refund minted 300)


# ── withdraw / payout ────────────────────────────────────────────────────────────
def test_withdraw_replayed_after_ttl_does_not_double_payout():
    clk = Clock()
    svc = _svc(clk)
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    first = run(svc.withdraw(tenant="t1", amount=400, destination="iban", source="w1"))
    assert _bal(svc) == 600
    _past_ttl(clk)
    again = run(svc.withdraw(tenant="t1", amount=400, destination="iban", source="w1"))  # retried
    assert again.get("idempotent") is True
    assert again["withdrawal_id"] == first["withdrawal_id"]     # the SAME payout, not a second one
    assert _bal(svc) == 600            # pre-fix: 200 (a second PENDING withdrawal debited again)
    assert len(svc._store._data.get("withdrawals", {})) == 1


# ── reclaim (durable via grant.state) ────────────────────────────────────────────
def test_reclaim_replayed_after_ttl_credits_a_dormant_grant_once():
    clk = Clock()
    svc = _svc(clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    gid = next(iter(svc._store._data["grants"]))
    clk.t += 31 * DAY
    run(svc.sweep_grants())                                 # -> dormant, swept=1000, funds=0
    run(svc.reclaim(grant_id=gid, source="job1"))
    assert _bal(svc) == 1000
    _past_ttl(clk)
    again = run(svc.reclaim(grant_id=gid, source="job1"))   # replayed after TTL
    assert again.get("already") is True
    assert _bal(svc) == 1000            # pre-fix intent: durable grant.state blocks a re-credit
