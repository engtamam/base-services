"""Redis Queue — infra-gated, lazy import (list-backed FIFO)."""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.queue import Job


class RedisQueue:
    driver = "redis"

    def __init__(self, url: str) -> None:
        if not url:
            raise ConfigError("QUEUE_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        self._client: Any = redis.from_url(url, decode_responses=True)

    def _k(self, queue: str) -> str:
        return f"queue:{queue}"

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        job = Job(id=uuid.uuid4().hex, queue=queue, payload=dict(payload))
        await self._client.rpush(self._k(queue), json.dumps({"id": job.id, "payload": job.payload}))
        return job.id

    async def dequeue(self, queue: str) -> Job | None:
        raw = await self._client.lpop(self._k(queue))
        if raw is None:
            return None
        data = json.loads(raw)
        return Job(id=data["id"], queue=queue, payload=data["payload"])

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
