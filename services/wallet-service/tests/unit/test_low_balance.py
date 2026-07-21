"""The low-balance warning: fire BEFORE the money runs out, once, and mean it.

The only "you are low" this service had was a 402 at spend time, which is not a warning — it is an
outage for whoever was mid-task. The load-bearing cases here are the ones about restraint and about
loss: an alert that fires every spend is noise and gets muted; an alert delivered only as an event
is no alert at all, because the bus is at-most-once with no replay.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import events as ev
from app.core.events import EMITTED, Emitter
from app.core.wallet_service import LOW_EVERY_SPEND, WalletService

from tests.unit.test_wallet_core import CFG, Clock


def run(c):
    return asyncio.run(c)


class _Bus:
    def __init__(self, fail: bool = False) -> None:
        self.published: list = []
        self._fail = fail

    async def publish(self, topic, event):
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.published.append((topic, event))

    def types(self) -> list[str]:
        return [t for t, _ in self.published]

    def of(self, kind: str) -> list:
        return [e for t, e in self.published if t == kind]


def _svc(bus, store=None, clock=None, **over):
    return WalletService(store=store or MemoryDataStore(), clock=clock or Clock(),
                         config={**CFG, **over}, event_bus=bus)


def _wallet(bus, store=None, threshold=1000, **over):
    return _svc(bus, store=store, low_balance_threshold=threshold, **over)


# ── the envelope ──────────────────────────────────────────────────────────────
def test_an_event_is_published_as_an_event_not_a_dict():
    """The port and every adapter type this as `Event`. A raw dict reaches a subscriber that does
    `event.type` and raises inside the handler, where the bus swallows it — so the publisher looks
    successful and the consumer never acts."""
    bus = _Bus()
    run(Emitter(bus).emit(ev.BALANCE_LOW, {"tenant": "acme"}))
    topic, event = bus.published[0]
    assert topic == ev.BALANCE_LOW
    assert event.type == ev.BALANCE_LOW
    assert event.payload == {"tenant": "acme"}
    assert event.id, "an event with no id cannot be de-duplicated by a consumer"


def test_an_undeclared_event_is_refused():
    with pytest.raises(ValueError, match="not declared"):
        run(Emitter(_Bus()).emit("wallet.invented", {}))


def test_with_no_bus_wired_emitting_is_a_no_op():
    run(Emitter(None).emit(ev.BALANCE_LOW, {}))


def test_by_default_a_broker_outage_does_not_break_the_spend():
    """The money has already moved, and the state is readable on the balance anyway."""
    bus = _Bus(fail=True)
    w = _wallet(bus)
    run(w.credit(tenant="acme", fund="cash", amount=2000, source="c1"))
    run(w.spend(tenant="acme", net=1500, source="s1"))   # must not raise


def test_fail_closed_is_available_for_platforms_that_want_it():
    with pytest.raises(RuntimeError):
        run(Emitter(_Bus(fail=True), fail_closed=True).emit(ev.BALANCE_LOW, {}))


def test_the_failure_policy_comes_from_config():
    w = _wallet(_Bus(), event_publish_failure=ev.FAIL_CLOSED)
    assert w._events._fail_closed is True
    assert _wallet(_Bus())._events._fail_closed is False


# ── the crossing, and only the crossing ───────────────────────────────────────
def test_the_warning_fires_on_the_crossing():
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    assert ev.BALANCE_LOW not in bus.types(), "5000 is above the line; nothing to warn about"
    run(w.spend(tenant="acme", net=3600, source="s1"))   # 5000 -> 860 gross, under 1000
    assert bus.of(ev.BALANCE_LOW), "a spend that took the balance under the line must warn"
    low = bus.of(ev.BALANCE_LOW)[0]
    assert low.payload["tenant"] == "acme" and low.payload["threshold"] == 1000


def test_it_does_not_fire_again_on_every_subsequent_spend():
    """An alert that fires every time is noise, and noise gets muted — which is how the ONE that
    mattered goes unread."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(w.spend(tenant="acme", net=3600, source="s1"))
    for i in range(4):
        run(w.spend(tenant="acme", net=100, source=f"s{i + 2}"))
    assert len(bus.of(ev.BALANCE_LOW)) == 1


def test_it_re_arms_only_after_recovery_and_warns_again():
    """A customer who tops up and drops again is a customer who must be warned again."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(w.spend(tenant="acme", net=3600, source="s1"))          # crossing #1
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c2"))   # recovered
    run(w.spend(tenant="acme", net=4400, source="s2"))          # crossing #2
    assert len(bus.of(ev.BALANCE_LOW)) == 2
    assert len(bus.of(ev.BALANCE_RECOVERED)) == 1


def test_recovery_is_emitted_and_not_merely_declared():
    """A declared event nothing publishes leaves a subscriber waiting for ever."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=500, source="c1"))    # starts low
    assert bus.of(ev.BALANCE_LOW)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c2"))
    rec = bus.of(ev.BALANCE_RECOVERED)
    assert rec and rec[0].payload["threshold"] == 1000


def test_recovery_alone_never_warns():
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    assert bus.types() == [ev.WALLET_CREDITED]


# ── the armed state is PERSISTED, not held in memory ──────────────────────────
def test_a_second_replica_does_not_warn_about_the_same_crossing():
    """Held in process memory, every replica warns separately about one crossing and a restart
    re-warns — which is exactly the noise the crossing rule exists to prevent."""
    store, clock, bus = MemoryDataStore(), Clock(), _Bus()
    first = _wallet(bus, store=store, threshold=1000)
    run(first.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(first.spend(tenant="acme", net=3600, source="s1"))
    assert len(bus.of(ev.BALANCE_LOW)) == 1

    # A different process object, same store — a restart, or a second pod.
    second = WalletService(store=store, clock=clock, event_bus=bus,
                           config={**CFG, "low_balance_threshold": 1000})
    run(second.spend(tenant="acme", net=100, source="s2"))
    assert len(bus.of(ev.BALANCE_LOW)) == 1, "the state must live in the store, not in the process"


def test_the_armed_state_is_written_to_the_store():
    store, bus = MemoryDataStore(), _Bus()
    w = _wallet(bus, store=store, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=500, source="c1"))
    row = run(store.get("wallet_alerts", "acme"))
    assert row and row["low"] is True and row["threshold"] == 1000


# ── off by default ────────────────────────────────────────────────────────────
def test_a_threshold_of_zero_warns_about_nothing():
    """0 = off. A general platform opts IN; a threshold guessed on an adopter's behalf is the
    wrong number for someone, and an unwanted alert is one they learn to mute."""
    store, bus = MemoryDataStore(), _Bus()
    w = _wallet(bus, store=store, threshold=0)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(w.spend(tenant="acme", net=4000, source="s1"))
    assert ev.BALANCE_LOW not in bus.types()
    assert run(store.get("wallet_alerts", "acme")) is None, "opted out: no bookkeeping either"
    assert run(w.balance("acme"))["low_balance"] is False


def test_with_the_feature_off_even_an_emptied_wallet_stays_quiet():
    """0 must mean OFF, not "warn when it hits zero". With the feature off the 402 remains the only
    signal — by the adopter's choice, which is the whole point of an opt-in."""
    bus = _Bus()
    w = _wallet(bus, threshold=0)
    run(w.credit(tenant="acme", fund="promo", amount=1000, source="c1"))   # untaxed: no VAT
    run(w.spend(tenant="acme", net=1000, source="s1"))
    assert run(w.balance("acme"))["balance"] == 0
    assert ev.BALANCE_LOW not in bus.types()
    assert run(w.balance("acme"))["low_balance"] is False


def test_a_negative_threshold_is_treated_as_off():
    bus = _Bus()
    w = _wallet(bus, threshold=-500)
    run(w.credit(tenant="acme", fund="cash", amount=1, source="c1"))
    assert ev.BALANCE_LOW not in bus.types()


# ── every_spend mode, implemented for real ────────────────────────────────────
def test_every_spend_mode_nags_on_each_debit():
    bus = _Bus()
    w = _wallet(bus, threshold=1000, low_balance_mode=LOW_EVERY_SPEND)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(w.spend(tenant="acme", net=3600, source="s1"))
    run(w.spend(tenant="acme", net=100, source="s2"))
    run(w.spend(tenant="acme", net=100, source="s3"))
    assert len(bus.of(ev.BALANCE_LOW)) == 3


def test_every_spend_mode_still_does_not_nag_on_credits():
    """A top-up that leaves you under the line is not a spend, and repeating the warning while the
    customer is actively paying is the fastest way to get muted."""
    bus = _Bus()
    w = _wallet(bus, threshold=5000, low_balance_mode=LOW_EVERY_SPEND)
    run(w.credit(tenant="acme", fund="cash", amount=100, source="c1"))
    run(w.credit(tenant="acme", fund="cash", amount=100, source="c2"))
    assert len(bus.of(ev.BALANCE_LOW)) == 1


# ── readable, because an event may simply never arrive ────────────────────────
def test_the_balance_read_carries_the_state_so_a_lost_event_is_survivable():
    """The bus is at-most-once with no replay. A consumer that missed `wallet.balance_low` would
    otherwise never learn it — not late, never. A read is self-healing."""
    w = _wallet(_Bus(), threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=500, source="c1"))
    b = run(w.balance("acme"))
    assert b["low_balance"] is True and b["low_balance_threshold"] == 1000
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c2"))
    assert run(w.balance("acme"))["low_balance"] is False


def test_the_read_is_right_even_with_no_bus_at_all():
    w = WalletService(store=MemoryDataStore(), clock=Clock(),
                      config={**CFG, "low_balance_threshold": 1000})
    run(w.credit(tenant="acme", fund="cash", amount=100, source="c1"))
    assert run(w.balance("acme"))["low_balance"] is True


# ── which fund the line is drawn on ───────────────────────────────────────────
def test_the_threshold_can_be_scoped_to_one_fund():
    """A promo gift that will expire is not the money that pays the bills."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000, low_balance_fund="cash")
    run(w.credit(tenant="acme", fund="promo", amount=9000, source="c1"))
    assert bus.of(ev.BALANCE_LOW), "cash is 0; a pile of promo does not pay the bill"
    run(w.credit(tenant="acme", fund="cash", amount=9000, source="c2"))
    assert bus.of(ev.BALANCE_RECOVERED)


# ── money-code paranoia ───────────────────────────────────────────────────────
def test_exactly_at_the_threshold_is_not_low():
    """The boundary is stated once, here: at-or-above the line is fine, below it warns."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=1000, source="c1"))
    assert ev.BALANCE_LOW not in bus.types()
    run(w.spend(tenant="acme", net=1, source="s1"))
    assert bus.of(ev.BALANCE_LOW)


def test_a_zero_amount_credit_neither_warns_nor_recovers():
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    run(w.credit(tenant="acme", fund="cash", amount=0, source="c2"))
    assert ev.BALANCE_LOW not in bus.types() and ev.BALANCE_RECOVERED not in bus.types()


def test_a_threshold_raised_mid_flight_warns_a_wallet_that_is_now_below_it():
    """The operator just declared this balance low. Carrying the old answer forward would mean the
    threshold they configured warns nobody until the customer happens to top up first."""
    store, bus = MemoryDataStore(), _Bus()
    run(_wallet(bus, store=store, threshold=1000).credit(
        tenant="acme", fund="cash", amount=5000, source="c1"))
    assert ev.BALANCE_LOW not in bus.types()
    raised = _wallet(bus, store=store, threshold=100_000)
    run(raised.spend(tenant="acme", net=1, source="s1"))
    assert bus.of(ev.BALANCE_LOW)
    assert run(raised.balance("acme"))["low_balance"] is True


def test_a_huge_balance_stays_an_integer():
    """Minor units are integers all the way down; a float here rounds someone's money away."""
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    big = 10 ** 15
    run(w.credit(tenant="acme", fund="cash", amount=big, source="c1"))
    b = run(w.balance("acme"))
    assert b["balance"] == big and isinstance(b["balance"], int)
    assert b["low_balance"] is False


def test_an_expiring_grant_that_drops_the_balance_warns():
    """The commonest way a balance falls without anyone spending. A sweep that takes a customer
    under the line silently is the same outage, arriving by cron."""
    store, clock, bus = MemoryDataStore(), Clock(), _Bus()
    w = _wallet(bus, store=store, clock=clock, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1",
                 expires_at=clock.now() + 10))
    clock.t += 100
    run(w.sweep_grants())
    assert bus.of(ev.BALANCE_LOW)


def test_a_refund_can_recover_the_wallet():
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    out = run(w.spend(tenant="acme", net=3600, source="s1"))
    assert bus.of(ev.BALANCE_LOW)
    run(w.refund(tenant="acme", seq=out["entry"]["seq"], source="r1"))
    assert bus.of(ev.BALANCE_RECOVERED)


def test_a_confirmed_hold_warns_on_the_crossing():
    bus = _Bus()
    w = _wallet(bus, threshold=1000)
    run(w.credit(tenant="acme", fund="cash", amount=5000, source="c1"))
    h = run(w.reserve(tenant="acme", net=3600, source="s1"))
    assert ev.BALANCE_LOW not in bus.types(), "a hold has not spent anything yet"
    run(w.confirm(hold_id=h["hold_id"]))
    assert bus.of(ev.BALANCE_LOW)


# ── the contract ──────────────────────────────────────────────────────────────
def test_the_manifest_and_the_code_declare_the_same_events():
    from app.config import Settings
    from app.manifest import build_manifest

    declared = build_manifest(Settings(INTERNAL_SERVICE_TOKEN="t" * 40))["events"]["emitted"]
    assert set(declared) == set(EMITTED)


def test_every_declared_event_is_actually_emitted_somewhere():
    """A declared event nothing publishes is a promise to a consumer that will never be kept."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "app" / "core" / "wallet_service.py"
    body = src.read_text(encoding="utf-8")
    constants = {v: k for k, v in vars(ev).items()
                 if isinstance(v, str) and v in EMITTED and k.isupper()}
    missing = [name for value, name in constants.items() if f"_events.{name}" not in body]
    assert not missing, f"declared but nothing emits them: {missing}"
