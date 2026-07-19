"""The watcher over a guardian consent reaching the age of majority.

capacity.py sets expires_at to the day the subject can speak for themself, and that was the whole
of majority handling. On their eighteenth birthday check() began answering "expired" and nothing
else happened — no notice to the controller, no prompt for the young adult's own consent.
Processing stopped, and from the outside it looked like a bug.

The heaviest tests here are about NOT announcing: an event that repeats every night is one a
consumer learns to ignore, and the consumer of these is likely to email a human.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.consent_sweeper import ConsentSweeper

_DAY = 86400.0
_NOTICE = 30 * _DAY


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def now(self):
        return self.t


class _Bus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.broken = False

    async def publish(self, event, payload):
        if self.broken:
            raise RuntimeError("bus down")
        self.published.append((event, payload))


def _world(notice=_NOTICE):
    store = MemoryDataStore()
    clock = _Clock()
    bus = _Bus()
    sweeper = ConsentSweeper(store=store, consents=None, clock=clock, event_bus=bus,
                             notice_seconds=notice, max_per_run=1000)
    return store, clock, bus, sweeper


def _consent(store, *, expires_at, basis="guardian", granted=True, subject="kid@example.com"):
    row = {"tenant": "acme", "subject": subject, "purpose": "coaching",
           "basis": basis, "granted": granted, "expires_at": expires_at}
    run(store.put("consent", f"acme::{subject}::coaching", row))
    return row


def _events(bus):
    return [e for e, _ in bus.published]


# ── the notice window, which is the point ──────────────────────────────────────
def test_a_consent_approaching_majority_announces_lapsing():
    """THE product value. This is the window in which a re-consent flow can run and the subject
    never experiences an interruption at all."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() + 10 * _DAY)

    out = run(sweeper.run())
    assert _events(bus) == ["consent.lapsing"]
    assert out["lapsing"] == 1


def test_nothing_is_announced_while_the_date_is_far_away():
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() + 200 * _DAY)
    run(sweeper.run())
    assert bus.published == []


def test_a_consent_past_majority_announces_lapsed():
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY)

    out = run(sweeper.run())
    assert _events(bus) == ["consent.lapsed"]
    assert out["lapsed"] == 1


def test_the_payload_carries_what_a_consumer_needs_to_act():
    """A product receiving this has to be able to find the person and ask them. An event that
    only says 'something lapsed' cannot start a re-consent flow."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY)
    run(sweeper.run())

    _, payload = bus.published[0]
    assert payload["subject"] == "kid@example.com"
    assert payload["purpose"] == "coaching"
    assert payload["tenant"] == "acme"
    assert payload["basis"] == "guardian"


# ── not announcing, which matters more ─────────────────────────────────────────
def test_a_second_run_announces_NOTHING_new():
    """Idempotent by state on the record, not by timing. A nightly cron that re-sent the same
    warning every night would train every consumer to filter it out."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY)

    run(sweeper.run())
    run(sweeper.run())
    run(sweeper.run())
    assert _events(bus) == ["consent.lapsed"]


def test_lapsing_then_lapsed_are_both_announced_exactly_once():
    """The two events are a sequence, not alternatives — but neither repeats."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() + 10 * _DAY)

    run(sweeper.run())                 # lapsing
    run(sweeper.run())                 # nothing new
    clock.t += 20 * _DAY               # the birthday passes
    run(sweeper.run())                 # lapsed
    run(sweeper.run())                 # nothing new

    assert _events(bus) == ["consent.lapsing", "consent.lapsed"]


def test_a_SELF_consent_is_never_swept():
    """An adult consenting for themself has no majority date. This sweeper is only about the one
    date the SUBJECT did not choose and cannot see coming."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY, basis="self")
    run(sweeper.run())
    assert bus.published == []


def test_a_withdrawn_consent_is_not_announced():
    """It is already not in force. Telling someone it is about to end would be nonsense."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY, granted=False)
    run(sweeper.run())
    assert bus.published == []


def test_a_consent_with_no_expiry_is_left_alone():
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=0.0)
    run(sweeper.run())
    assert bus.published == []


def test_the_early_notice_can_be_switched_off():
    """0 disables it, leaving only consent.lapsed — honest but late, and that is the operator's
    call rather than ours."""
    store, clock, bus, sweeper = _world(notice=0.0)
    _consent(store, expires_at=clock.now() + 10 * _DAY)
    run(sweeper.run())
    assert bus.published == []


def test_a_broken_bus_does_not_crash_the_sweep():
    """One undeliverable event must not stop the run — the next consent still needs looking at."""
    store, clock, bus, sweeper = _world()
    bus.broken = True
    _consent(store, expires_at=clock.now() - _DAY)
    out = run(sweeper.run())
    assert out["lapsed"] == 1


def test_a_legal_representative_lapse_is_swept_too():
    """Not only guardians. The delegated bases share the property that the subject did not agree."""
    store, clock, bus, sweeper = _world()
    _consent(store, expires_at=clock.now() - _DAY, basis="legal_representative")
    run(sweeper.run())
    assert _events(bus) == ["consent.lapsed"]
