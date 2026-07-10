"""Local flag provider — we own the definitions in the DataStore (the default)."""
from __future__ import annotations

from typing import Any

from app.ports.datastore import DataStore

COLLECTION = "flags"


class LocalFlags:
    driver = "local"

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def get(self, key: str) -> dict[str, Any] | None:
        return await self._store.get(COLLECTION, key)

    async def list(self, limit: int = 500) -> list[dict[str, Any]]:
        return (await self._store.list(COLLECTION, limit))[:limit]
