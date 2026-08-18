"""Queue contract — the promise every driver must keep.

The port claimed "at-least-once semantics for real backends" while offering only enqueue/dequeue.
At-least-once needs the consumer to be able to say *I have finished*, and there was no way to say
it: the Redis adapter was `LPOP`, so a worker killed mid-job took the job with it — no retry, no
error, no trace. The queue was empty and every counter agreed.

These tests are the guarantee written down. A driver that passes them cannot lose a job to a dead
worker; a driver that cannot pass them does not belong behind this port.
"""
from __future__ import annotations

import os

import pytest

from app.adapters.queue.memory import MemoryQueue
from tests.contracts.util import run

# The redis adapter is held to the SAME contract against a REAL server whenever REDIS_TEST_URL is set,
# and SKIPS VISIBLY otherwise (mirrors the datastore contract's DATASTORE_TEST_DSN):
#   docker run -d -p 6379:6379 redis:7-alpine
#   REDIS_TEST_URL=redis://127.0.0.1:6379/0 pytest tests/contracts
_REDIS_URL = os.environ.get("REDIS_TEST_URL", "")


def _memory():
    return MemoryQueue()


def _redis():
    from app.adapters.queue.redis import RedisQueue
    return RedisQueue(_REDIS_URL)


_PARAMS = [pytest.param(_memory, id="memory")]
_PARAMS.append(pytest.param(_redis, id="redis") if _REDIS_URL else pytest.param(
    _redis, id="redis", marks=pytest.mark.skip(reason="set REDIS_TEST_URL to run the redis contract")))

LEASE = 30


@pytest.fixture(params=_PARAMS)
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
    a = run(queue.dequeue("jobs", lease_seconds=LEASE))
    b = run(queue.dequeue("jobs", lease_seconds=LEASE))
    assert a.payload == {"n": 1}
    assert b.payload == {"n": 2}


def test_empty_returns_none(queue):
    assert run(queue.dequeue("empty", lease_seconds=LEASE)) is None


def test_a_leased_job_is_invisible_to_other_workers(queue):
    """Otherwise two workers scan the same file at once — the lease is what makes it a lease."""
    run(queue.enqueue("jobs", {"n": 1}))
    first = run(queue.dequeue("jobs", lease_seconds=LEASE))
    assert first is not None
    assert run(queue.dequeue("jobs", lease_seconds=LEASE)) is None


def test_a_dead_worker_does_not_take_the_job_with_it():
    """THE BUG. Dequeue, never ack (the worker died), let the lease expire: the job comes back."""
    clock = {"t": 1000.0}
    q = MemoryQueue(now=lambda: clock["t"])
    run(q.enqueue("jobs", {"n": 1}))
    run(q.dequeue("jobs", lease_seconds=LEASE))          # worker takes it, then is killed

    assert run(q.dequeue("jobs", lease_seconds=LEASE)) is None    # still held, correctly
    clock["t"] += LEASE + 1
    assert run(q.reclaim("jobs")) == 1

    back = run(q.dequeue("jobs", lease_seconds=LEASE))
    assert back is not None and back.payload == {"n": 1}


def test_an_acked_job_never_comes_back():
    """The other half: at-least-once must not become for-ever."""
    clock = {"t": 1000.0}
    q = MemoryQueue(now=lambda: clock["t"])
    run(q.enqueue("jobs", {"n": 1}))
    job = run(q.dequeue("jobs", lease_seconds=LEASE))
    assert run(q.ack(job)) is True

    clock["t"] += LEASE + 1
    assert run(q.reclaim("jobs")) == 0
    assert run(q.dequeue("jobs", lease_seconds=LEASE)) is None


def test_nack_returns_the_job_immediately(queue):
    """A known failure should retry now, not after the lease elapses."""
    run(queue.enqueue("jobs", {"n": 1}))
    job = run(queue.dequeue("jobs", lease_seconds=LEASE))
    assert run(queue.nack(job)) is True
    again = run(queue.dequeue("jobs", lease_seconds=LEASE))
    assert again is not None and again.payload == {"n": 1}


def test_a_worker_that_lost_its_lease_cannot_ack():
    """FENCING. The old holder may still be running. Its ack must not delete the job somebody
    else is now working on — that would resurrect the original bug with extra steps."""
    clock = {"t": 1000.0}
    q = MemoryQueue(now=lambda: clock["t"])
    run(q.enqueue("jobs", {"n": 1}))
    stale = run(q.dequeue("jobs", lease_seconds=LEASE))

    clock["t"] += LEASE + 1
    run(q.reclaim("jobs"))
    fresh = run(q.dequeue("jobs", lease_seconds=LEASE))       # a second worker picks it up

    assert run(q.ack(stale)) is False, "a lost lease still deleted the job"
    assert run(q.ack(fresh)) is True


def test_a_zero_lease_is_refused(queue):
    """A zero lease IS a pop. Refusing it stops the old behaviour returning by the back door."""
    run(queue.enqueue("jobs", {"n": 1}))
    with pytest.raises(ValueError):
        run(queue.dequeue("jobs", lease_seconds=0))


def test_reclaim_only_touches_expired_leases():
    clock = {"t": 1000.0}
    q = MemoryQueue(now=lambda: clock["t"])
    run(q.enqueue("jobs", {"n": 1}))
    run(q.dequeue("jobs", lease_seconds=LEASE))
    clock["t"] += LEASE - 1          # still inside the lease
    assert run(q.reclaim("jobs")) == 0


def test_reclaim_does_not_reach_across_queues():
    clock = {"t": 1000.0}
    q = MemoryQueue(now=lambda: clock["t"])
    run(q.enqueue("scan", {"n": 1}))
    run(q.dequeue("scan", lease_seconds=LEASE))
    clock["t"] += LEASE + 1
    assert run(q.reclaim("export")) == 0
    assert run(q.reclaim("scan")) == 1
