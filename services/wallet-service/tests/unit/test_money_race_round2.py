"""Round-2 concurrency-money regressions (docs/audits/round2/wallet-service.json).

Each test FAILS on the pre-fix code. Concurrency is forced with an InterleavingStore whose get()
yields the event loop (`await asyncio.sleep(0)`), so coroutines gathered on one wallet interleave
their read-modify-write and expose a missing atomic claim. Money is integer minor units throughout.

  wallet-r2-01  release() / sweep_holds() must take the per-hold claim confirm/settle take, or two
                concurrent releases both decrement `held`, under-count it, and raid another hold.
  wallet-r2-02  reclaim() must key its claim on the GRANT (not the caller source) and re-read state,
                or two distinct-source reclaims of one dormant grant both credit (mint).
  wallet-r2-04  sweep_grants() must claim per grant, or two overlapping sweeps double-debit an
                expiring grant and claw money reserved for another still-active grant.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.wallet_service import WalletService

DAY = 86400
FUNDS = (("cash", 1, True, True),)
CFG = {"funds": FUNDS, "vat_bp": 0, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 20}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


class InterleavingStore(MemoryDataStore):
    """Yields the loop on every get() so gathered coroutines interleave their read-modify-write."""

    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _svc(store=None, clock=None, policies=None):
    cfg = {**CFG}
    if policies is not None:
        cfg["fund_expiry_policies"] = policies
    return WalletService(store=store or InterleavingStore(), clock=clock or Clock(), config=cfg)


# ── wallet-r2-01 ────────────────────────────────────────────────────────────────
def test_concurrent_release_of_one_hold_does_not_raid_another_hold():
    svc = _svc()
    run(svc.credit(tenant="t1", fund="cash", amount=200, source="c1"))
    run(svc.reserve(tenant="t1", net=50, source="r2"))              # H2: 50 held, must survive
    h1 = run(svc.reserve(tenant="t1", net=100, source="r1"))["hold_id"]
    # held == 150 now (100 + 50).

    async def two_releases():
        return await asyncio.gather(svc.release(hold_id=h1), svc.release(hold_id=h1),
                                    return_exceptions=True)

    run(two_releases())
    bal = run(svc.balance("t1"))
    # Pre-fix: both releases decrement held (150->50, then 50-100 clamped to 0) -> held == {} and the
    # freed 100 raids H2's 50. Fixed: released once, H2's 50 still reserved.
    assert bal["held"].get("cash", 0) == 50
    assert bal["available"].get("cash", 0) == 150
    assert bal["funds"].get("cash", 0) == 200                       # nothing was ever debited


def test_release_racing_sweep_holds_frees_a_hold_only_once():
    clk = Clock()
    svc = _svc(clock=clk)
    run(svc.credit(tenant="t1", fund="cash", amount=200, source="c1"))
    h1 = run(svc.reserve(tenant="t1", net=100, source="r1"))["hold_id"]  # expires at clk+900
    clk.t += 1_000                                                  # H1 now expired (sweepable)
    run(svc.reserve(tenant="t1", net=50, source="r2"))             # H2 fresh -> NOT expired, survivor

    async def race():
        return await asyncio.gather(svc.release(hold_id=h1), svc.sweep_holds(),
                                    return_exceptions=True)

    run(race())
    bal = run(svc.balance("t1"))
    # release and sweep both target H1. Pre-fix each decrements held (150->50, then 50-100 clamped 0)
    # -> H2's 50 is raided. Fixed: the shared per-hold claim frees H1 once; H2's 50 stays reserved.
    assert bal["held"].get("cash", 0) == 50
    assert bal["available"].get("cash", 0) == 150
    assert bal["funds"].get("cash", 0) == 200


# ── wallet-r2-02 ────────────────────────────────────────────────────────────────
def test_two_distinct_source_reclaims_credit_a_dormant_grant_once():
    clk = Clock()
    svc = _svc(clock=clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "dormant"}})
    run(svc.credit(tenant="t1", fund="cash", amount=1000, source="c1"))
    gid = next(iter(svc._store._data["grants"]))                    # the only grant
    clk.t += 31 * DAY
    run(svc.sweep_grants())                                         # -> dormant, swept=1000, funds=0

    async def two_reclaims():
        return await asyncio.gather(svc.reclaim(grant_id=gid, source="jobA"),
                                    svc.reclaim(grant_id=gid, source="jobB"),
                                    return_exceptions=True)

    run(two_reclaims())
    bal = run(svc.balance("t1"))
    # Pre-fix: distinct-source claims both win and both read the stale dormant state -> 2000 minted.
    assert bal["funds"].get("cash", 0) == 1000


# ── wallet-r2-04 ────────────────────────────────────────────────────────────────
def test_overlapping_sweeps_do_not_double_debit_an_expiring_grant():
    clk = Clock()
    svc = _svc(clock=clk, policies={"cash": {"ttl_days": 30, "idle_behavior": "forfeit"}})
    # Two grants share the cash fund; funds == 200. g1 expires now, g2 stays active (far future).
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="c1"))   # -> g1
    grants = svc._store._data["grants"]
    g1 = next(iter(grants))
    run(svc.credit(tenant="t1", fund="cash", amount=100, source="c2",
                   expires_at=clk.t + 365 * DAY))                        # -> g2, still active
    clk.t += 31 * DAY                                                    # only g1 is due

    async def two_sweeps():
        return await asyncio.gather(svc.sweep_grants(), svc.sweep_grants(),
                                    return_exceptions=True)

    run(two_sweeps())
    bal = run(svc.balance("t1"))
    # Pre-fix: both runs read g1 active+expired and each debit 100 -> funds 0, clawing g2's reserved
    # 100. Fixed: g1 forfeited exactly once; g2's 100 survives.
    assert bal["funds"].get("cash", 0) == 100
    assert grants[g1]["state"] == "expired"
