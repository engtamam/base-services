"""In-memory Queue — zero infra (FIFO)."""
from __future__ import annotations

import uuid
from collections import deque
from typing import Any

from app.ports.queue import Job


class MemoryQueue:
    driver = "memory"

    def __init__(self) -> None:
        self._queues: dict[str, deque[Job]] = {}

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        job = Job(id=uuid.uuid4().hex, queue=queue, payload=dict(payload))
        self._queues.setdefault(queue, deque()).append(job)
        return job.id

    async def dequeue(self, queue: str) -> Job | None:
        q = self._queues.get(queue)
        if not q:
            return None
        return q.popleft()

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._queues.clear()
