"""Cache port — key/value cache with optional TTL."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    driver: str

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        """Atomically increment the integer at `key` and return the NEW value. Creates it at 1 when
        absent, stamping `ttl_seconds` on that first increment (later increments keep the TTL). The
        atomicity is the point: a read-modify-write on a plain get/set loses concurrent increments,
        which lets a brute-force fan-out exceed an attempt ceiling."""
        ...

    async def delete(self, key: str) -> bool: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
