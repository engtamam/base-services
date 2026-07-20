"""In-memory Queue — zero infra (FIFO), with the same lease semantics as the real backends.

The memory driver is what every test and every zero-infrastructure deployment runs, so if it
merely popped while Redis leased, the guarantee would be tested nowhere and the two drivers would
disagree about the one property that matters. A fake that is easier than the real thing is a fake
that stops catching the bugs.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from typing import Any, Callable

from app.ports.queue import Job


class MemoryQueue:
    driver = "memory"

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._queues: dict[str, deque[Job]] = {}
        # id -> (job, expires_at). A leased job lives HERE, not in the queue: invisible to other
        # workers, and still recorded, which is the whole difference from a pop.
        self._leased: dict[str, tuple[Job, float]] = {}
        self._now = now

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        job = Job(id=uuid.uuid4().hex, queue=queue, payload=dict(payload))
        self._queues.setdefault(queue, deque()).append(job)
        return job.id

    async def dequeue(self, queue: str, *, lease_seconds: int) -> Job | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive — a zero lease is a pop, which is "
                             "the bug this port exists to prevent")
        q = self._queues.get(queue)
        if not q:
            return None
        job = q.popleft()
        leased = Job(id=job.id, queue=job.queue, payload=job.payload, token=uuid.uuid4().hex)
        self._leased[job.id] = (leased, self._now() + lease_seconds)
        return leased

    async def ack(self, job: Job) -> bool:
        held = self._leased.get(job.id)
        # Token equality, not mere presence: a worker whose lease expired and was reclaimed must
        # not delete the job the NEW holder is now working on.
        if held is None or held[0].token != job.token:
            return False
        del self._leased[job.id]
        return True

    async def nack(self, job: Job) -> bool:
        held = self._leased.get(job.id)
        if held is None or held[0].token != job.token:
            return False
        del self._leased[job.id]
        # Back to the FRONT: a known failure is retried before newer work, so a poison message
        # surfaces immediately instead of hiding behind a busy queue.
        self._queues.setdefault(job.queue, deque()).appendleft(
            Job(id=job.id, queue=job.queue, payload=job.payload))
        return True

    async def reclaim(self, queue: str) -> int:
        now = self._now()
        expired = [jid for jid, (job, exp) in self._leased.items()
                   if job.queue == queue and exp <= now]
        for jid in expired:
            job, _ = self._leased.pop(jid)
            self._queues.setdefault(queue, deque()).append(
                Job(id=job.id, queue=job.queue, payload=job.payload))
        return len(expired)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._queues.clear()
        self._leased.clear()
