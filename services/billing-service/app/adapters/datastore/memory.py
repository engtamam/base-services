"""In-memory DataStore — zero infra. Deep-copies values to avoid aliasing."""
from __future__ import annotations

import asyncio
import copy
from typing import Any


class MemoryDataStore:
    driver = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        # Serialises read-modify-write. A single process still interleaves at every `await`,
        # so `n = get(); put(n+1)` races between concurrent requests without this.
        self._lock = asyncio.Lock()

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        row = self._data.get(collection, {}).get(key)
        return copy.deepcopy(row) if row is not None else None

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = copy.deepcopy(value)

    async def delete(self, collection: str, key: str) -> bool:
        return self._data.get(collection, {}).pop(key, None) is not None

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(self._data.get(collection, {}).values())
        return copy.deepcopy(rows[: max(0, limit)])

    async def incr_if_below(self, collection: str, key: str, delta: int,
                            limit: int | None) -> tuple[bool, int]:
        """Check-and-increment as ONE indivisible step (see the port's docstring)."""
        async with self._lock:
            bucket = self._data.setdefault(collection, {})
            current = int(bucket.get(key, {}).get("n", 0))
            after = current + int(delta)
            if delta < 0:
                after = max(0, after)                     # a refund is never refused
            elif limit is not None and after > int(limit):
                return False, current                     # would exceed — change nothing
            bucket[key] = {"n": after}
            return True, after

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
