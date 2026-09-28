"""Per-fund grant expiry policy — infrastructure the platform configures per fund.

forfeit (service credit) | dormant (SAMA-safe, reclaimable) | extend; per-fund TTL frozen at credit;
a one-time pre-expiry notice; reclaim() restores a dormant grant. Money stays integer, ledger-backed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import CLAWBACK, EXPIRE, RECLAIM
from app.core.wallet_service import WalletService, WalletError

FUNDS = (("promo", 1, False, False), ("cash", 2, True, True))   # (name, pri, taxed, withdrawable)
CFG = {"funds": FUNDS, "vat_bp": 1500, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8}
DAY = 86400


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


def _svc(clock, policies):
    return WalletService(store=MemoryDataStore(), clock=clock,
                         config={**CFG, "fund_expiry_policies": policies})


async def _ledger_types(svc, tenant):
    return [e["type"] for e in await svc.ledger(tenant, limit=100)]


def test_credit_freezes_expiry_from_the_fund_policy():
    clk = Clock()
    svc = _svc(clk, {"promo": {"ttl_days": 365, "idle_behavior": "forfeit"}})
    r = run(svc.credit(tenant="t1", fund="promo", amount=1000, source="c1"))
    assert "grant_id" in r                              # a grant with a frozen expiry was created
    # cash has no policy → no grant/expiry
    r2 = run(svc.credit(tenant="t1", fund="cash", amount=500, source="c2"))
    assert "grant_id" not in r2


def test_forfeit_claws_back_and_is_irreversible():
    clk = Clock()
    svc = _svc(clk, {"promo": {"ttl_days": 30, "idle_behavior": "forfeit"}})
    run(svc.credit(tenant="t1", fund="promo", amount=1000, source="c1"))
    clk.t += 31 * DAY
    out = run(svc.sweep_grants())
    assert out["expired"] == 1 and out["clawed"] == 1000 and out["dormant"] == 0
    assert run(svc.balance("t1"))["funds"].get("promo", 0) == 0
    assert CLAWBACK in run(_ledger_types(svc, "t1"))


def test_dormant_preserves_and_is_reclaimable():
    clk = Clock()
    svc = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    r = run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    gid = r["grant_id"]
    clk.t += 31 * DAY
    out = run(svc.sweep_grants())
    assert out["dormant"] == 1000 and out["clawed"] == 0
    assert run(svc.balance("t1"))["funds"].get("cash", 0) == 0    # left spendable
    assert EXPIRE in run(_ledger_types(svc, "t1"))               # preserved, traceable — not clawback
    # reclaim restores it
    rec = run(svc.reclaim(grant_id=gid, source="rc1"))
    assert rec["reclaimed"] == 1000
    assert run(svc.balance("t1"))["funds"]["cash"] == 1000
    assert RECLAIM in run(_ledger_types(svc, "t1"))


def test_reclaim_is_idempotent_and_refuses_a_non_dormant_grant():
    clk = Clock()
    svc = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    gid = run(svc.credit(tenant="t1", fund="cash", amount=800, source="c1"))["grant_id"]
    with pytest.raises(WalletError):                    # active, not dormant yet
        run(svc.reclaim(grant_id=gid, source="rc1"))
    clk.t += 31 * DAY
    run(svc.sweep_grants())
    first = run(svc.reclaim(grant_id=gid, source="rc1"))
    again = run(svc.reclaim(grant_id=gid, source="rc1"))  # same source → idempotent, no double credit
    assert first["reclaimed"] == 800 and again["reclaimed"] == 800 and again.get("idempotent") is True
    assert run(svc.balance("t1"))["funds"]["cash"] == 800   # credited once, not twice


def test_extend_pushes_expiry_without_moving_money():
    clk = Clock()
    svc = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "extend"}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    clk.t += 31 * DAY
    out = run(svc.sweep_grants())
    assert out["extended"] == 1 and out["expired"] == 0
    assert run(svc.balance("t1"))["funds"]["cash"] == 1000       # untouched
    # still active, now expires ~30 days out again
    assert run(svc.sweep_grants())["extended"] == 0             # not due again yet


def test_notice_fires_once_before_expiry():
    clk = Clock()
    svc = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "dormant", "notice_days": 7}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    assert run(svc.notice_grants())["noticed"] == 0            # 30 days out, outside the 7-day window
    clk.t += 24 * DAY                                          # now 6 days from expiry
    assert run(svc.notice_grants())["noticed"] == 1
    assert run(svc.notice_grants())["noticed"] == 0           # once only


def test_no_policy_expiry_is_dormant_not_forfeit():
    # wallet-002: a caller-supplied expires_at on a fund with NO policy must NOT silently FORFEIT the
    # stored value. It freezes the safe 'dormant' default (expired-but-reclaimable), so nothing is
    # clawed back and the customer can reclaim the remainder.
    clk = Clock()
    svc = _svc(clk, {})                                        # no policies at all
    run(svc.credit(tenant="t1", fund="promo", amount=1000, source="c1", expires_at=clk.t + 10))
    clk.t += 20
    out = run(svc.sweep_grants())
    assert out["expired"] == 1 and out["clawed"] == 0          # NOT forfeited
    assert CLAWBACK not in run(_ledger_types(svc, "t1"))       # no clawback ledger entry


def test_policy_change_never_touches_an_already_issued_grant():
    clk = Clock()
    svc = _svc(clk, {"promo": {"ttl_days": 10, "idle_behavior": "forfeit"}})
    run(svc.credit(tenant="t1", fund="promo", amount=1000, source="c1"))
    # the grant's expiry is frozen at credit time (now + 10d); a later, longer policy cannot rescue it
    svc._policies = {"promo": {"ttl_days": 999, "idle_behavior": "forfeit"}}
    clk.t += 11 * DAY
    assert run(svc.sweep_grants())["expired"] == 1            # frozen 10-day expiry still applied


def test_idle_behavior_is_frozen_so_a_later_change_cannot_forfeit_a_dormant_grant():
    # The exact scenario the design must prevent: credit under dormant, later flip the fund to forfeit
    # (or delete the policy). The already-issued grant MUST still be preserved/reclaimable, not forfeited.
    clk = Clock()
    svc = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    gid = run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))["grant_id"]
    svc._policies = {"cash": {"ttl_days": 30, "idle_behavior": "forfeit"}}  # policy flipped after credit
    clk.t += 31 * DAY
    out = run(svc.sweep_grants())
    assert out["dormant"] == 1000 and out["clawed"] == 0     # honoured the FROZEN dormant, not the new forfeit
    rec = run(svc.reclaim(grant_id=gid, source="rc1"))       # still reclaimable
    assert rec["reclaimed"] == 1000

    # and the same holds when the fund's policy is DELETED entirely after credit
    svc2 = _svc(clk, {"cash": {"ttl_days": 30, "idle_behavior": "dormant", "notice_days": 5}})
    g2 = run(svc2.credit(tenant="t2", fund="cash", amount=500, source="c2"))["grant_id"]
    svc2._policies = {}                                       # policy gone
    clk.t += 31 * DAY
    assert run(svc2.sweep_grants())["dormant"] == 500        # frozen dormant survives a deleted policy
    assert run(svc2.reclaim(grant_id=g2, source="rc2"))["reclaimed"] == 500
