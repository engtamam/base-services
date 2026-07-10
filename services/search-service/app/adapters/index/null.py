"""Null index — stores nothing, returns nothing. Search yields empty results (fail-safe)."""
from __future__ import annotations

from typing import Any


class NullIndex:
    driver = "null"

    async def upsert(self, **kwargs) -> None:
        return None

    async def delete(self, **kwargs) -> None:
        return None

    async def get(self, **kwargs) -> dict[str, Any] | None:
        return None

    async def postings(self, **kwargs) -> dict[str, float]:
        return {}

    async def stats(self, **kwargs) -> dict[str, Any]:
        return {"n_docs": 0, "total_len": 0.0}

    async def iter_docs(self, **kwargs) -> list[dict[str, Any]]:
        return []
