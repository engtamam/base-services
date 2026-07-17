"""Queue contract."""
from __future__ import annotations

import pytest

from app.adapters.queue.memory import MemoryQueue
from tests.contracts.util import run


@pytest.fixture(params=[MemoryQueue])
def queue(request):
    q = request.param()
    yield q
    run(q.close())


def test_enqueue_returns_id(queue):
    jid = run(queue.enqueue("jobs", {"n": 1}))
    assert isinstance(jid, str) and jid


def test_fifo_order(queue):
    run(queue.enqueue("jobs", {"n": 1}))
    run(queue.enqueue("jobs", {"n": 2}))
    a = run(queue.dequeue("jobs"))
    b = run(queue.dequeue("jobs"))
    assert a.payload == {"n": 1}
    assert b.payload == {"n": 2}


def test_empty_returns_none(queue):
    assert run(queue.dequeue("empty")) is None
