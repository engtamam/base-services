"""In-memory DataStore — zero infra. Deep-copies values to avoid aliasing."""
from __future__ import annotations

import copy
from typing import Any


class MemoryDataStore:
    driver = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

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

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
