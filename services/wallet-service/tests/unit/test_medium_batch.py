"""Non-security MEDIUM remediation (wallet-service):
  004  idle_behavior='extend' with ttl_days==0 is a config error (an extend that can never extend).
  007  reconcile checks held == sum(applied over active counted holds), not only funds==sum(ledger).
  011  the low-balance threshold measures AVAILABLE (funds - held), not raw funds.
  022  a withdrawable fund credited with an explicit expires_at and no policy is PRESERVED, not clawed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import CLAWBACK, EXPIRE
from app.core.wallet_service import WalletService
from app.ports.errors import ConfigError

FUNDS = (("promo", 1, False, False), ("cash", 2, True, True))   # (name, pri, taxed, withdrawable)
CFG = {"funds": FUNDS, "vat_bp": 1500, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8}
DAY = 86400


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def now(self): return self.t


def _svc(clock=None, **over):
    return WalletService(store=MemoryDataStore(), clock=clock or Clock(), config={**CFG, **over})


# ── 004: extend + ttl_days=0 is a config error ────────────────────────────────
def test_extend_with_zero_ttl_is_rejected():
    from app.config import Settings
    s = Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40,
                 WALLET_FUND_EXPIRY_POLICIES='{"cash": {"ttl_days": 0, "idle_behavior": "extend"}}')
    with pytest.raises(ConfigError):
        _ = s.fund_expiry_policies


def test_extend_with_positive_ttl_is_fine():
    from app.config import Settings
    s = Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40,
                 WALLET_FUND_EXPIRY_POLICIES='{"cash": {"ttl_days": 30, "idle_behavior": "extend"}}')
    assert s.fund_expiry_policies["cash"]["idle_behavior"] == "extend"


# ── 007: reconcile verifies the held invariant ────────────────────────────────
def test_reconcile_reports_held_consistency():
    svc = _svc()
    run(svc.credit(tenant="t", fund="cash", amount=1000, source="c"))
    run(svc.reserve(tenant="t", net=100, source="r"))     # creates an active, counted hold
    rec = run(svc.reconcile("t"))
    assert rec["consistent"] is True
    assert rec["held_consistent"] is True
    assert rec["held_stored"] == rec["held_derived"]      # held == sum(applied over active holds)
    assert sum(rec["held_derived"].values()) > 0


# ── 011: low-balance measures available, not raw funds ────────────────────────
def test_low_balance_measures_available_not_raw_funds():
    svc = _svc(low_balance_threshold=500)
    run(svc.credit(tenant="t", fund="cash", amount=1000, source="c"))
    # raw funds = 1000 (above 500), but an ~800-net hold (gross ~920) drops AVAILABLE below the threshold
    run(svc.reserve(tenant="t", net=800, source="r"))
    b = run(svc.balance("t"))
    assert b["balance"] == 1000                            # raw funds unchanged
    assert b["low_balance"] is True                        # but available is low → warned


# ── 022: a withdrawable fund with a caller expires_at is preserved, not clawed ─
def test_withdrawable_fund_with_explicit_expiry_is_preserved_not_clawed():
    clk = Clock()
    svc = _svc(clk)                                        # NO fund_expiry_policies configured
    r = run(svc.credit(tenant="t", fund="cash", amount=1000, source="c1",
                       expires_at=clk.now() + 10 * DAY))
    assert "grant_id" in r
    clk.t += 11 * DAY
    out = run(svc.sweep_grants())
    assert out["clawed"] == 0 and out["dormant"] == 1000   # preserved (SAMA-safe), never forfeited
    types = [e["type"] for e in run(svc.ledger("t", limit=100))]
    assert EXPIRE in types and CLAWBACK not in types
