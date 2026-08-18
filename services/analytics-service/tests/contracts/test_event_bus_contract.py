"""EventBus contract."""
from __future__ import annotations

import os

import pytest

from app.adapters.event_bus.memory import MemoryEventBus
from app.ports.event_bus import Event
from tests.contracts.util import run

# The redis adapter is held to the SAME contract against a REAL server whenever REDIS_TEST_URL is set,
# and SKIPS VISIBLY otherwise (mirrors the datastore contract's DATASTORE_TEST_DSN):
#   docker run -d -p 6379:6379 redis:7-alpine
#   REDIS_TEST_URL=redis://127.0.0.1:6379/0 pytest tests/contracts
_REDIS_URL = os.environ.get("REDIS_TEST_URL", "")


def _memory():
    return MemoryEventBus()


def _redis():
    from app.adapters.event_bus.redis import RedisEventBus
    return RedisEventBus(_REDIS_URL)


_PARAMS = [pytest.param(_memory, id="memory")]
_PARAMS.append(pytest.param(_redis, id="redis") if _REDIS_URL else pytest.param(
    _redis, id="redis", marks=pytest.mark.skip(reason="set REDIS_TEST_URL to run the redis contract")))


@pytest.fixture(params=_PARAMS)
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
