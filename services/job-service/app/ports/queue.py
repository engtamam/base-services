"""Queue port — async job queue (at-least-once semantics for real backends)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Job:
    id: str
    queue: str
    payload: dict[str, Any]


@runtime_checkable
class Queue(Protocol):
    driver: str

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        """Enqueue a job; return its id."""
        ...

    async def dequeue(self, queue: str) -> Job | None:
        """Pop the next job, or None if the queue is empty."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
