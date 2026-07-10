"""In-process EventBus — zero infra. Handler errors are isolated."""
from __future__ import annotations

from app.ports.event_bus import Event, Handler


class MemoryEventBus:
    driver = "memory"

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    async def publish(self, topic: str, event: Event) -> None:
        for handler in list(self._handlers.get(topic, ())):
            try:
                await handler(event)
            except Exception:  # one subscriber must not break others / the publisher
                pass

    async def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._handlers.clear()
