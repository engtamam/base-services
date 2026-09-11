"""In-memory Cache with TTL — zero infra."""
from __future__ import annotations

import time


class MemoryCache:
    driver = "memory"

    def __init__(self) -> None:
        # key -> (value, expires_at_monotonic | None)
        self._data: dict[str, tuple[str, float | None]] = {}

    def _expired(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return True
        _, exp = entry
        if exp is not None and time.monotonic() >= exp:
            self._data.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._data[key][0]

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        exp = time.monotonic() + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, exp)

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        # Single-threaded asyncio with no await inside: this read-modify-write is atomic by
        # construction, matching redis INCR's guarantee for the memory driver.
        if self._expired(key):
            exp = time.monotonic() + ttl_seconds if ttl_seconds else None
            self._data[key] = ("1", exp)
            return 1
        value, exp = self._data[key]                      # keep the existing expiry across increments
        nxt = int(value) + 1
        self._data[key] = (str(nxt), exp)
        return nxt

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
