"""Audit HIGH: a forfeit grant sharing a fund with PERMANENT money (a no-expiry top-up, or money
returned by reclaim) must never claw that permanent money. The clawback protects a per-fund
non-forfeitable floor; it may forfeit less than theoretically possible (customer-favourable) but
never confiscates the customer's own balance.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

DAY = 86400
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


def _svc(clock, policies=None):
    cfg = {**CFG}
    if policies is not None:
        cfg["fund_expiry_policies"] = policies
    return WalletService(store=MemoryDataStore(), clock=clock, config=cfg)


def _cash(svc):
    return run(svc.balance("t1"))["funds"].get("cash", 0)


def test_forfeit_grant_never_claws_a_permanent_topup_in_a_shared_fund():
    clk = Clock()
    svc = _svc(clk, policies={})                       # `cash` has NO default policy
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="topup"))          # permanent (no expiry)
    run(svc.credit(tenant="t1", fund="cash", amount=100, expires_at=clk.now() + 10))  # a forfeit grant
    assert _cash(svc) == 200
    run(svc.spend(tenant="t1", net=100, source="s1"))     # spend the grant's worth from the pool
    assert _cash(svc) == 100
    clk.t += 100                                          # the grant expires
    run(svc.sweep_grants())
    assert _cash(svc) == 100          # the permanent top-up SURVIVES (pre-fix: clawed to 0)


def test_forfeit_grant_claws_only_its_own_surplus_above_the_permanent_floor():
    clk = Clock()
    svc = _svc(clk, policies={})
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="topup"))          # permanent 100
    run(svc.credit(tenant="t1", fund="cash", amount=100, expires_at=clk.now() + 10))  # grant 100, unspent
    assert _cash(svc) == 200
    clk.t += 100
    run(svc.sweep_grants())
    assert _cash(svc) == 100          # the grant's full 100 is clawed, the permanent 100 kept


def test_reclaimed_money_becomes_permanent_and_is_not_clawed_by_a_later_grant():
    clk = Clock()
    svc = _svc(clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="c1"))   # dormant grant (reclaimable)
    gid = next(iter(svc._store._data["grants"]))
    clk.t += 31 * DAY
    run(svc.sweep_grants())                               # -> dormant, cash 0
    run(svc.reclaim(grant_id=gid, source="job1"))         # reclaim -> cash 100, now PERMANENT
    assert _cash(svc) == 100
    # a later forfeit grant in the same fund, then expire it: the reclaimed money must survive
    run(svc.credit(tenant="t1", fund="cash", amount=50, expires_at=clk.now() + 10))
    assert _cash(svc) == 150
    run(svc.spend(tenant="t1", net=50, source="s2"))      # spend the new grant's worth
    clk.t += 100
    run(svc.sweep_grants())
    assert _cash(svc) == 100          # reclaimed permanent 100 survives (pre-fix: could be clawed)
