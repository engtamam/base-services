"""Redis RateLimiter — infra-gated, lazy import (fixed-window via INCR/EXPIRE)."""
from __future__ import annotations

from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError


class RedisRateLimiter:
    driver = "redis"

    def __init__(self, url: str, max_requests: int, window_seconds: int) -> None:
        if not url:
            raise ConfigError("RATELIMIT_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        self._client: Any = redis.from_url(url, decode_responses=True)
        self._max = max(1, max_requests)
        self._window = max(1, window_seconds)

    def _k(self, key: str) -> str:
        return f"ratelimit:{key}"

    async def allow(self, key: str) -> bool:
        k = self._k(key)
        count = await self._client.incr(k)
        if count == 1:
            await self._client.expire(k, self._window)
        return count <= self._max

    async def reset(self, key: str) -> None:
        await self._client.delete(self._k(key))

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
