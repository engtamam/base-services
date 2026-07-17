"""EventBus contract."""
from __future__ import annotations

import pytest

from app.adapters.event_bus.memory import MemoryEventBus
from app.ports.event_bus import Event
from tests.contracts.util import run


@pytest.fixture(params=[MemoryEventBus])
def bus(request):
    b = request.param()
    yield b
    run(b.close())


def test_subscribe_then_publish_delivers(bus):
    received: list[Event] = []

    async def handler(ev: Event) -> None:
        received.append(ev)

    async def scenario() -> None:
        await bus.subscribe("topic.a", handler)
        await bus.publish("topic.a", Event(type="x", payload={"n": 1}))

    run(scenario())
    assert len(received) == 1
    assert received[0].payload == {"n": 1}


def test_no_subscriber_is_noop(bus):
    run(bus.publish("topic.none", Event(type="x", payload={})))


def test_handler_error_isolated(bus):
    async def bad(_ev: Event) -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        await bus.subscribe("t", bad)
        await bus.publish("t", Event(type="x", payload={}))  # must not raise

    run(scenario())
