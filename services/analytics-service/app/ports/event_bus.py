"""EventBus port — publish/subscribe for domain events."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any]
    id: str = ""
    correlation_id: str = ""
    ts: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    driver: str

    async def publish(self, topic: str, event: Event) -> None: ...

    async def subscribe(self, topic: str, handler: Handler) -> None: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
