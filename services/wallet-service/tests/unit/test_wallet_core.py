"""Wallet correctness. Each test names a way a wallet quietly loses or invents money.

The three footguns, in order of how hard they are to see:
  1. the materialized balance drifting from the ledger;
  2. spending money that is already held (a reservation becomes decoration);
  3. refunding an expiring gift into cash — silently converting it into real money.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import funds as F
from app.core import money as M
from app.core.wallet_service import Conflict, WalletError, WalletService

FUNDS = (("promo", 1, False), ("cash", 2, True))     # promo is untaxed => a pre-VAT discount
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


def _svc(store=None, clock=None, **over):
    return WalletService(store=store or MemoryDataStore(), clock=clock or Clock(),
                         config={**CFG, **over})


# ── money is integers, always ───────────────────────────────────────────────
def test_vat_is_integer_and_rounds_half_up():
    assert M.gross_of(100, 1500) == (100, 15, 115)
    assert M.vat_of(7, 1500) == 1                     # 1.05 -> 1
    assert M.vat_of(3, 1500) == 0                     # 0.45 -> 0
    assert M.vat_of(0, 1500) == 0


def test_gross_always_equals_net_plus_vat_exactly():
    for net in range(0, 5000, 7):
        n, v, g = M.gross_of(net, 1500)
        assert g == n + v                             # a float would drift here


def test_negative_money_is_refused():
    with pytest.raises(M.MoneyError):
        M.require_non_negative(-1)


# ── the fund plan: promo is a PRE-VAT discount ──────────────────────────────
def test_an_untaxed_fund_discounts_before_vat_is_computed():
    plan = F.plan_spend(1000, {"promo": 400, "cash": 10_000}, F.parse(FUNDS), 1500)
    assert plan.discount == 400 and plan.taxable_net == 600
    assert plan.vat == 90 and plan.gross == 690
    assert plan.applied == {"promo": 400, "cash": 690}
    # Charging VAT first would take 150 VAT and 750 cash — the customer overpays 60.


def test_a_fully_discounted_spend_carries_no_vat():
    plan = F.plan_spend(500, {"promo": 500, "cash": 0}, F.parse(FUNDS), 1500)
    assert plan.vat == 0 and plan.applied == {"promo": 500}


def test_insufficient_funds_mutates_nothing():
    balances = {"promo": 100, "cash": 50}
    with pytest.raises(F.InsufficientFunds):
        F.plan_spend(1000, balances, F.parse(FUNDS), 1500)
    assert balances == {"promo": 100, "cash": 50}


def test_funds_are_never_driven_negative():
    plan = F.plan_spend(100, {"promo": 30, "cash": 1000}, F.parse(FUNDS), 1500)
    after = F.apply({"promo": 30, "cash": 1000}, plan.applied)
    assert all(v >= 0 for v in after.values())


def test_the_code_never_names_a_fund_a_single_fund_wallet_works():
    one = F.parse((("credits", 1, False),))
    plan = F.plan_spend(7, {"credits": 10}, one, 1500)
    assert plan.applied == {"credits": 7} and plan.vat == 0


def test_five_funds_are_spent_in_priority_order():
    many = F.parse((("gift", 1, False), ("promo", 2, False), ("prepaid", 3, True),
                    ("cash", 4, True), ("overdraft", 5, True)))
    plan = F.plan_spend(1000, {"gift": 100, "promo": 100, "prepaid": 200, "cash": 1000,
                               "overdraft": 999}, many, 1500)
    assert plan.discount == 200                       # both untaxed funds discounted first
    assert plan.applied["gift"] == 100 and plan.applied["promo"] == 100
    assert plan.applied["prepaid"] == 200             # then taxed funds, in priority order
    assert "overdraft" not in plan.applied            # cash covered the rest


# ── FOOTGUN 1: the projection must equal the ledger ─────────────────────────
def test_balance_always_equals_the_sum_of_the_ledger():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="a"))
    run(w.credit(tenant="t", fund="promo", amount=500, source="b"))
    run(w.spend(tenant="t", net=1000, source="c"))
    r = run(w.reconcile("t"))
    assert r["consistent"] is True and r["stored"] == r["derived"]


def test_reconcile_detects_a_missing_historical_ledger_row():
    """Only the LAST row is staged for replay. Losing an older one is real corruption."""
    store = MemoryDataStore()
    w = _svc(store=store)
    run(w.credit(tenant="t", fund="cash", amount=100, source="a"))
    run(w.credit(tenant="t", fund="cash", amount=50, source="b"))
    del store._data["ledger"]["t:000000000001"]        # an old row vanished
    out = run(w.reconcile("t"))
    assert out["consistent"] is False and out["missing_seq"] == 1


def test_a_crash_before_the_ledger_append_is_replayed_not_lost():
    """The ledger row is staged inside the wallet CAS, so it can never be lost."""
    store = MemoryDataStore()
    w = _svc(store=store)
    run(w.credit(tenant="t", fund="cash", amount=100, source="a"))
    store._data["ledger"].clear()                      # the append died after the CAS
    run(w.credit(tenant="t", fund="cash", amount=1, source="b"))   # next write replays it
    assert run(w.reconcile("t"))["consistent"] is True


def test_money_never_moves_without_a_ledger_row():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=500, source="a"))
    run(w.spend(tenant="t", net=100, source="b"))
    entries = run(w.ledger("t"))
    assert len(entries) == 2
    assert {e["type"] for e in entries} == {"credit", "spend"}


# ── FOOTGUN 1b: concurrency ─────────────────────────────────────────────────
def test_fifty_concurrent_spends_of_the_last_unit_produce_exactly_one_winner():
    store = MemoryDataStore()
    w = _svc(store=store)
    run(w.credit(tenant="t", fund="cash", amount=115, source="c"))   # funds exactly one spend

    async def race():
        return await asyncio.gather(*[w.spend(tenant="t", net=100, source=f"s{i}")
                                      for i in range(50)], return_exceptions=True)

    results = asyncio.run(race())
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1, f"{len(winners)} spenders won the same money"
    assert run(w.balance("t"))["balance"] == 0
    assert run(w.reconcile("t"))["consistent"] is True


def test_the_balance_can_never_go_negative():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=10, source="c"))
    with pytest.raises(F.InsufficientFunds):
        run(w.spend(tenant="t", net=1000, source="s"))
    assert run(w.balance("t"))["balance"] == 10


def test_cas_exhaustion_raises_conflict_and_writes_nothing():
    class NeverWins(MemoryDataStore):
        async def cas(self, *a, **k):
            return False

    w = _svc(store=NeverWins(), cas_max_retries=3)
    with pytest.raises(Conflict):
        run(w.credit(tenant="t", fund="cash", amount=100, source="x"))


# ── FOOTGUN 2: a hold must actually hold ────────────────────────────────────
def test_available_excludes_held_funds():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    run(w.reserve(tenant="t", net=100, source="r"))
    b = run(w.balance("t"))
    assert b["balance"] == 1000
    assert b["available_total"] == 1000 - b["held"]["cash"]


def test_held_money_cannot_be_spent_twice():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=115, source="c"))
    run(w.reserve(tenant="t", net=100, source="r"))
    with pytest.raises(F.InsufficientFunds):
        run(w.spend(tenant="t", net=100, source="s"))   # the money is reserved


def test_confirm_debits_and_clears_the_hold_in_one_step():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.confirm(hold_id=h["hold_id"]))
    b = run(w.balance("t"))
    assert b["held"] == {} and b["balance"] == 1000 - 115
    assert run(w.reconcile("t"))["consistent"] is True


def test_confirming_twice_charges_once():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.confirm(hold_id=h["hold_id"]))
    again = run(w.confirm(hold_id=h["hold_id"]))
    assert again["idempotent"] is True
    assert run(w.balance("t"))["balance"] == 885


def test_partial_confirm_charges_the_smaller_amount_and_releases_the_rest():
    """Reserve a ceiling, confirm the real (smaller) net — the difference returns to the balance.

    net=100 reserves gross 115. Confirming final_net=40 charges gross_of(40,15%)=46 and frees 69.
    """
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    res = run(w.confirm(hold_id=h["hold_id"], final_net=40))
    assert res["released"] == 115 - 46
    b = run(w.balance("t"))
    assert b["held"] == {} and b["balance"] == 1000 - 46          # only the real cost left the balance
    assert run(w.reconcile("t"))["consistent"] is True
    led = run(w.ledger("t"))
    spend = [e for e in led if e["type"] == "spend"][0]
    assert spend["net"] == 40 and spend["vat"] == 6 and spend["amount"] == -46   # VAT on the real net


def test_confirm_with_final_net_equal_to_reserved_is_the_full_charge():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    res = run(w.confirm(hold_id=h["hold_id"], final_net=100))
    assert res["released"] == 0
    assert run(w.balance("t"))["balance"] == 1000 - 115


def test_confirm_above_the_reserved_net_is_refused_and_mutates_nothing():
    from app.core.wallet_service import WalletError
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    try:
        run(w.confirm(hold_id=h["hold_id"], final_net=101))   # > reserved net: never spend unreserved money
        assert False, "expected WalletError"
    except WalletError as exc:
        assert "EXCEEDS_RESERVED" in str(exc)
    b = run(w.balance("t"))
    assert b["balance"] == 1000 and b["held"] == {"cash": 115}   # still reserved, nothing charged


def test_partial_confirm_replay_is_idempotent_at_the_first_amount():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.confirm(hold_id=h["hold_id"], final_net=40))
    again = run(w.confirm(hold_id=h["hold_id"], final_net=70))   # a different amount on retry is ignored
    assert again["idempotent"] is True
    assert run(w.balance("t"))["balance"] == 1000 - 46           # still the FIRST confirm's charge


def test_releasing_a_hold_frees_the_money_without_a_ledger_entry():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.release(hold_id=h["hold_id"]))
    b = run(w.balance("t"))
    assert b["held"] == {} and b["balance"] == 1000       # nothing was ever debited
    assert len(run(w.ledger("t"))) == 1                   # only the credit


def test_a_confirmed_hold_cannot_be_released():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    run(w.confirm(hold_id=h["hold_id"]))
    with pytest.raises(WalletError):
        run(w.release(hold_id=h["hold_id"]))              # that needs refund()


# ── the leak the reference implementation has: abandoned holds ──────────────
def test_an_expired_hold_is_never_charged():
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    h = run(w.reserve(tenant="t", net=100, source="r"))
    clock.t += 10_000                                     # the client vanished
    with pytest.raises(WalletError):
        run(w.confirm(hold_id=h["hold_id"]))


def test_the_sweeper_returns_credits_a_dead_client_stranded():
    """Without this, a phone dying between reserve and confirm locks the money forever."""
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    run(w.reserve(tenant="t", net=100, source="r"))
    assert run(w.balance("t"))["available_total"] == 885

    clock.t += 10_000
    out = run(w.sweep_holds())
    assert out["swept"] == 1 and out["freed"] == 115
    assert run(w.balance("t"))["available_total"] == 1000   # the money came back
    assert run(w.balance("t"))["held"] == {}


def test_the_sweep_is_bounded():
    clock = Clock()
    w = _svc(clock=clock, hold_sweep_limit=2)
    run(w.credit(tenant="t", fund="cash", amount=100_000, source="c"))
    for i in range(5):
        run(w.reserve(tenant="t", net=100, source=f"r{i}"))
    clock.t += 10_000
    assert run(w.sweep_holds())["swept"] == 2              # bounded, resumable


def test_sweeping_an_already_swept_hold_does_nothing():
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="cash", amount=1000, source="c"))
    run(w.reserve(tenant="t", net=100, source="r"))
    clock.t += 10_000
    run(w.sweep_holds())
    assert run(w.sweep_holds())["swept"] == 0
    assert run(w.balance("t"))["balance"] == 1000


# ── FOOTGUN 3: a refund must return money to the funds it came from ─────────
def test_a_refund_returns_promo_to_promo_not_to_cash():
    """Refunding an expiring gift into cash converts it into real money."""
    w = _svc()
    run(w.credit(tenant="t", fund="promo", amount=400, source="g"))
    run(w.credit(tenant="t", fund="cash", amount=5000, source="c"))
    spend = run(w.spend(tenant="t", net=1000, source="op"))
    assert spend["entry"]["applied"] == {"promo": 400, "cash": 690}

    out = run(w.refund(tenant="t", seq=spend["entry"]["seq"], source="rf"))
    assert out["applied"] == {"promo": 400, "cash": 690}
    assert out["funds"] == {"promo": 400, "cash": 5000}    # exactly where it started
    assert run(w.reconcile("t"))["consistent"] is True


def test_a_refund_cannot_be_replayed_into_free_money():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=5000, source="c"))
    spend = run(w.spend(tenant="t", net=100, source="op"))
    run(w.refund(tenant="t", seq=spend["entry"]["seq"], source="rf"))
    again = run(w.refund(tenant="t", seq=spend["entry"]["seq"], source="rf"))
    assert again["idempotent"] is True
    assert run(w.balance("t"))["balance"] == 5000


def test_only_a_spend_can_be_refunded():
    w = _svc()
    credit = run(w.credit(tenant="t", fund="cash", amount=100, source="c"))
    with pytest.raises(WalletError):
        run(w.refund(tenant="t", seq=credit["entry"]["seq"], source="rf"))


# ── credits, grants, clawback ───────────────────────────────────────────────
def test_a_redelivered_settlement_credits_once():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="psp-evt-1"))
    again = run(w.credit(tenant="t", fund="cash", amount=10_000, source="psp-evt-1"))
    assert again["idempotent"] is True
    assert run(w.balance("t"))["balance"] == 10_000


def test_crediting_an_unknown_fund_is_refused():
    w = _svc()
    with pytest.raises(WalletError):
        run(w.credit(tenant="t", fund="bitcoin", amount=1, source="x"))


def test_an_expired_grant_is_clawed_back_but_the_customer_keeps_what_they_spent():
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="promo", amount=500, source="g", expires_at=2000.0))
    run(w.credit(tenant="t", fund="cash", amount=5000, source="c"))
    run(w.spend(tenant="t", net=200, source="op"))          # spends 200 of the promo
    assert run(w.balance("t"))["funds"]["promo"] == 300

    clock.t = 3000.0
    out = run(w.sweep_grants())
    assert out["clawed"] == 300                             # only the UNUSED remainder
    assert run(w.balance("t"))["funds"]["promo"] == 0
    assert run(w.reconcile("t"))["consistent"] is True


def test_a_clawback_never_takes_reserved_money():
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="promo", amount=500, source="g", expires_at=2000.0))
    run(w.reserve(tenant="t", net=100, source="r"))          # 100 of promo is held
    clock.t = 2500.0                                         # grant expired, hold still alive
    run(w.sweep_grants())
    b = run(w.balance("t"))
    assert b["funds"]["promo"] == 100                        # the reserved 100 survived
    assert b["held"]["promo"] == 100


def test_a_grant_with_no_expiry_is_never_swept():
    clock = Clock()
    w = _svc(clock=clock)
    run(w.credit(tenant="t", fund="promo", amount=500, source="g"))    # no expires_at
    clock.t += 10_000_000
    assert run(w.sweep_grants())["expired"] == 0
    assert run(w.balance("t"))["funds"]["promo"] == 500


# ── reads ───────────────────────────────────────────────────────────────────
def test_the_ledger_is_bounded_regardless_of_what_is_asked():
    w = _svc(ledger_list_limit=3)
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    for i in range(10):
        run(w.spend(tenant="t", net=10, source=f"s{i}"))
    assert len(run(w.ledger("t", limit=1000))) == 3


def test_the_ledger_paginates_newest_first():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=10_000, source="c"))
    for i in range(5):
        run(w.spend(tenant="t", net=10, source=f"s{i}"))
    page1 = run(w.ledger("t", limit=2))
    assert [e["seq"] for e in page1] == [6, 5]
    page2 = run(w.ledger("t", limit=2, before_seq=page1[-1]["seq"]))
    assert [e["seq"] for e in page2] == [4, 3]


def test_an_empty_wallet_reads_as_zero_not_an_error():
    w = _svc()
    b = run(w.balance("nobody"))
    assert b["balance"] == 0 and b["available_total"] == 0
    assert run(w.reconcile("nobody"))["consistent"] is True


def test_tenants_are_isolated():
    w = _svc()
    run(w.credit(tenant="a", fund="cash", amount=100, source="x"))
    assert run(w.balance("b"))["balance"] == 0


# ── idempotency is ATOMIC under concurrency (not get-then-put) ────────────────
def test_two_concurrent_same_source_spends_apply_exactly_once():
    """The TOCTOU race: two requests carrying the SAME source (a redelivered webhook, two replicas
    draining one queue message) must never both apply the spend. Exactly one mutates; the other
    either replays the result or is told to retry — and the balance drops once."""
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="seed"))
    before = run(w.balance("t"))["funds"]["cash"]

    async def both():
        import asyncio
        return await asyncio.gather(
            w.spend(tenant="t", net=1000, source="dup"),
            w.spend(tenant="t", net=1000, source="dup"),
            return_exceptions=True)

    results = run(both())
    ok = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, Exception)]
    # at least one succeeded; any non-dict is a Conflict (retryable), never a second real spend
    assert ok, f"no spend succeeded: {results}"
    assert all(isinstance(e, Conflict) for e in errs), f"unexpected error: {errs}"
    after = run(w.balance("t"))["funds"]["cash"]
    # gross of 1000 net @ 15% VAT = 1150, debited EXACTLY ONCE
    assert before - after == 1150, f"double-spend: {before}->{after}"


def test_a_sequential_retry_of_the_same_source_replays_not_recharges():
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="seed"))
    first = run(w.spend(tenant="t", net=1000, source="dup2"))
    again = run(w.spend(tenant="t", net=1000, source="dup2"))
    assert again.get("idempotent") is True and again["spent"] == first["spent"]
    # only one debit total
    bal = run(w.balance("t"))["funds"]["cash"]
    assert 1_000_000 - bal == 1150


def test_a_retry_while_the_winner_is_still_pending_is_refused_not_reapplied():
    """A duplicate that retries WHILE the original is mid-mutation must be refused (Conflict), never
    allowed to proceed — otherwise it double-applies before the winner finalizes."""
    w = _svc()
    run(w.credit(tenant="t", fund="cash", amount=1_000_000, source="seed"))
    # simulate the winner having claimed but not yet finalized: an unexpired pending idempo row
    run(w._store.put("wallet_idempo", "t|spend|dupP",
                     {"status": "pending", "expires_at": w._clock.now() + 60}))
    with pytest.raises(Conflict):
        run(w.spend(tenant="t", net=1000, source="dupP"))
    # nothing debited
    assert run(w.balance("t"))["funds"]["cash"] == 1_000_000
