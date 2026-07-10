"""Cache port — key/value cache with optional TTL."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    driver: str

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
