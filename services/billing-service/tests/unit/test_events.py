"""The domain events billing publishes.

This service recorded money correctly for the whole of its life and told nobody, and 394 tests
passed throughout — because not one of them asserted that anything was published. These do.

The load-bearing cases are the ones about what an event CANNOT be trusted to do: the bus is
at-most-once and best effort, so a lost event must not lose money, and a broker outage must not
fail a payment that has already moved.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import events as ev
from app.core.events import EMITTED, Emitter, payment_payload


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


class _Inv:
    id = "inv_1"
    tenant = "acme"
    total_minor = 11500
    currency = "SAR"
    paid_at = "2026-07-21T00:00:00Z"


# ── the envelope ──────────────────────────────────────────────────────────────
def test_an_event_is_published_as_an_event_not_a_dict():
    """The port and every adapter type this as `Event`. A raw dict reaches a subscriber that does
    `event.type` and raises inside the handler, where the bus swallows it — so the publisher looks
    successful and the consumer never acts."""
    bus = _Bus()
    run(Emitter(bus).emit(ev.PAYMENT_SUCCEEDED, {"tenant": "acme"}))
    topic, event = bus.published[0]
    assert topic == ev.PAYMENT_SUCCEEDED
    assert event.type == ev.PAYMENT_SUCCEEDED
    assert event.payload == {"tenant": "acme"}
    assert event.id, "an event with no id cannot be de-duplicated by a consumer"


def test_an_undeclared_event_is_refused():
    """An event absent from the manifest reaches nobody by design: a consumer cannot subscribe to
    something it cannot discover. Refusing keeps the code and the declaration in step."""
    with pytest.raises(ValueError, match="not declared"):
        run(Emitter(_Bus()).emit("payment.invented", {}))


def test_with_no_bus_wired_emitting_is_a_no_op():
    # The service must record money correctly with or without anyone listening.
    run(Emitter(None).emit(ev.PAYMENT_SUCCEEDED, {}))


# ── a broker outage must not fail a payment ───────────────────────────────────
def test_by_default_a_broker_outage_does_not_break_the_payment():
    """The money has already moved. Refusing to record it because a broker is unreachable turns a
    degraded notification path into a failed payment."""
    run(Emitter(_Bus(fail=True)).emit(ev.PAYMENT_SUCCEEDED, {}))


def test_fail_closed_is_available_for_platforms_that_want_it():
    with pytest.raises(RuntimeError):
        run(Emitter(_Bus(fail=True), fail_closed=True).emit(ev.PAYMENT_SUCCEEDED, {}))


# ── the payload ───────────────────────────────────────────────────────────────
def test_the_payload_carries_the_payment_identity():
    """A consumer keys idempotency on this. Without it, a redelivery and a genuine second payment
    of the same amount are indistinguishable — and crediting twice is money given away."""
    p = payment_payload(_Inv(), payment_id="pay_9")
    assert p["payment_id"] == "pay_9"
    assert p["tenant"] == "acme" and p["invoice_id"] == "inv_1"
    assert p["amount_minor"] == 11500 and p["currency"] == "SAR"


def test_the_payload_carries_no_personal_data():
    """A payload lands in logs and in every subscriber's store, including subscribers with no
    lawful basis to hold a person's details. The tenant and invoice id are enough to look the rest
    up from a service that is allowed to."""
    p = payment_payload(_Inv(), payment_id="pay_9")
    for banned in ("buyer_name", "buyer_vat", "email", "address", "name"):
        assert banned not in p


def test_the_manifest_and_the_code_declare_the_same_events():
    """Two lists that must agree. If they drift, either an event nobody can discover is published
    or the manifest promises one that never fires — and a consumer waits for ever."""
    from app.config import Settings
    from app.manifest import build_manifest

    declared = build_manifest(Settings(INTERNAL_SERVICE_TOKEN="t" * 40))["events"]["emitted"]
    assert set(declared) == set(EMITTED)


def test_every_declared_event_is_actually_emitted_somewhere():
    """A declared event nothing publishes is a promise to a consumer that will never be kept.

    subscription.renewed was declared and unwired when this was written — the manifest advertised
    it while nothing fired it, which is the audit-wiring defect one layer out.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "app" / "core" / "billing_service.py"
    body = src.read_text(encoding="utf-8")
    constants = {v: k for k, v in vars(ev).items()
                 if isinstance(v, str) and v in EMITTED and k.isupper()}
    missing = [name for value, name in constants.items() if f"_events.{name}" not in body]
    assert not missing, f"declared but nothing emits them: {missing}"
