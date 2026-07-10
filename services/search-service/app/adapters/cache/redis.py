"""Redis Cache — infra-gated, lazy import."""
from __future__ import annotations

from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError


class RedisCache:
    driver = "redis"

    def __init__(self, url: str) -> None:
        if not url:
            raise ConfigError("CACHE_URL is required for the redis driver")
        redis = load("redis.asyncio", "redis")
        self._client: Any = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        await self._client.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(key))

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
