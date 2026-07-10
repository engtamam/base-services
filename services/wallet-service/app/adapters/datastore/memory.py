"""In-memory DataStore — zero infra. Deep-copies values to avoid aliasing."""
from __future__ import annotations

import asyncio
import copy
from typing import Any


class MemoryDataStore:
    driver = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        # A single process still interleaves at every `await`, so read-modify-write races
        # between concurrent requests without this.
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

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """See the port docstring. The compare and the swap are one indivisible step."""
        async with self._lock:
            bucket = self._data.setdefault(collection, {})
            current = bucket.get(key)
            current_version = int(current.get("version", 0)) if current else 0
            if current_version != int(expected_version):
                return False                                  # someone else wrote first
            stored = copy.deepcopy(value)
            stored["version"] = current_version + 1
            bucket[key] = stored
            return True

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
