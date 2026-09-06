"""Fund scoping — a spend for one purpose must NEVER drain another purpose's balance.

This is the cross-drain footgun. A wallet holds separate funds for separate purposes (an AI-usage
credit, a renewal top-up, ...). Today spend/reserve walk ALL declared funds in priority order, so a
call meant to burn the AI credit would silently eat the renewal money once the AI fund ran dry.

`funds=(...)` scopes a call to a named subset. Every test here names a way that scoping could leak,
be guessed, or overdraw:
  - a scoped spend draws ONLY its own fund, even when other funds could have covered it;
  - the refusal is scoped too: `available` counts the subset, not the wallet;
  - a refusal mutates nothing — the out-of-scope funds are untouched to the minor unit;
  - an empty scope and an unknown fund fail LOUD, never as a silent 'insufficient'.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import funds as F
from app.core.wallet_service import WalletError, WalletService

# Three purposes on one wallet. `ai` and `topup` are taxed real money; `promo` is an untaxed gift.
FUNDS = (("promo", 1, False), ("ai", 2, True), ("topup", 3, True))
CFG = {"funds": FUNDS, "vat_bp": 1500, "currency": "SAR", "hold_ttl": 900,
       "hold_sweep_limit": 500, "grant_sweep_limit": 500, "idempotency_ttl": 3600,
       "ledger_list_limit": 100, "cas_max_retries": 8}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def _svc():
    return WalletService(store=MemoryDataStore(), clock=Clock(), config=CFG)


def _seed(w, **funds):
    for name, amount in funds.items():
        run(w.credit(tenant="t", fund=name, amount=amount, source=f"seed-{name}"))


# ── the pure layer: plan_spend honours the allowlist ─────────────────────────

def test_plan_scoped_to_one_fund_ignores_the_others_entirely():
    # topup could pay, but the scope forbids it — the plan must refuse rather than reach for it.
    balances = {"ai": 100, "topup": 1_000_000}
    with pytest.raises(F.InsufficientFunds) as ei:
        F.plan_spend(1_000, balances, F.parse(FUNDS), 1500, allowed=frozenset({"ai"}))
    # `available` reports only the in-scope fund, an honest "your AI fund cannot cover this".
    assert ei.value.available == 100


def test_plan_scoped_spend_draws_only_the_named_fund():
    balances = {"ai": 10_000, "topup": 10_000}
    plan = F.plan_spend(1_000, balances, F.parse(FUNDS), 1500, allowed=frozenset({"ai"}))
    assert set(plan.applied) == {"ai"} and "topup" not in plan.applied


def test_an_out_of_scope_untaxed_fund_gives_no_discount():
    # promo is untaxed (a pre-VAT discount) but out of scope: the spend is taxed in full on `ai`.
    balances = {"promo": 10_000, "ai": 10_000}
    scoped = F.plan_spend(1_000, balances, F.parse(FUNDS), 1500, allowed=frozenset({"ai"}))
    unscoped = F.plan_spend(1_000, balances, F.parse(FUNDS), 1500)
    assert scoped.discount == 0 and scoped.vat > 0
    assert unscoped.discount == 1_000  # promo would have wiped the net, and thus the VAT


# ── the service layer: scope validation fails loud ───────────────────────────

def test_none_scope_is_the_historical_all_funds_behaviour():
    w = _svc()
    _seed(w, promo=10_000)
    out = run(w.spend(tenant="t", net=1_000, source="s1"))   # promo (untaxed) covers it fully
    assert out["plan"]["discount"] == 1_000


def test_an_empty_scope_is_refused_loudly():
    w = _svc()
    _seed(w, ai=10_000)
    with pytest.raises(WalletError, match="FUNDS_SCOPE_EMPTY"):
        run(w.spend(tenant="t", net=1_000, source="s1", funds=()))


def test_an_unknown_fund_name_is_refused_loudly():
    w = _svc()
    _seed(w, ai=10_000)
    with pytest.raises(WalletError, match="UNKNOWN_FUND:typo"):
        run(w.spend(tenant="t", net=1_000, source="s1", funds=("ai", "typo")))


def test_a_scoped_spend_never_touches_an_out_of_scope_fund():
    w = _svc()
    _seed(w, ai=100, topup=1_000_000)
    # scope to ai only; ai cannot cover 1_000 net + VAT, so this must fail — and leave topup intact.
    with pytest.raises(F.InsufficientFunds):
        run(w.spend(tenant="t", net=1_000, source="s1", funds=("ai",)))
    bal = run(w.balance("t"))["funds"]
    assert bal["ai"] == 100 and bal["topup"] == 1_000_000   # not one minor unit moved


def test_scoped_spend_debits_only_its_fund_when_it_can_cover():
    w = _svc()
    _seed(w, ai=10_000, topup=10_000)
    run(w.spend(tenant="t", net=1_000, source="s1", funds=("ai",)))
    bal = run(w.balance("t"))["funds"]
    assert bal["ai"] < 10_000 and bal["topup"] == 10_000   # topup untouched


def test_reserve_is_scoped_the_same_way():
    w = _svc()
    _seed(w, ai=100, topup=1_000_000)
    with pytest.raises(F.InsufficientFunds):
        run(w.reserve(tenant="t", net=1_000, source="h1", funds=("ai",)))
    bal = run(w.balance("t"))["funds"]
    assert bal["ai"] == 100 and bal["topup"] == 1_000_000
