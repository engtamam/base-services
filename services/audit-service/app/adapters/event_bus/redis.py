"""Redis EventBus — infra-gated, lazy import (pub/sub)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.event_bus import Event, Handler


class RedisEventBus:
    driver = "redis"

    def __init__(self, url: str) -> None:
        if not url:
            raise ConfigError("EVENTBUS_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        self._client: Any = redis.from_url(url, decode_responses=True)
        self._tasks: list[asyncio.Task] = []

    async def publish(self, topic: str, event: Event) -> None:
        await self._client.publish(topic, json.dumps(dataclasses.asdict(event)))

    async def subscribe(self, topic: str, handler: Handler) -> None:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(topic)

        async def _reader() -> None:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    await handler(Event(**data))
                except Exception:
                    pass

        self._tasks.append(asyncio.create_task(_reader()))

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._client.aclose()
