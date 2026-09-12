"""In-memory DataStore — zero infra. Deep-copies values to avoid aliasing."""
from __future__ import annotations

import copy
from typing import Any

from app.adapters.datastore import cursor as _cursor
from app.ports.datastore import SORT_DIRECTIONS
from app.ports.errors import ConfigError


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

    async def add_fields(self, collection: str, key: str, deltas: dict[str, Any],
                         set_fields: dict[str, Any] | None = None) -> None:
        """Atomic additive upsert. There is NO await between the read and the write, so on the single
        event loop this whole method runs to completion before another coroutine can interleave —
        exactly the property a get()+put() pair lacks. Missing counters start at 0."""
        bucket = self._data.setdefault(collection, {})
        row = dict(bucket.get(key) or {})
        for field, delta in deltas.items():
            row[field] = int(row.get(field, 0) or 0) + int(delta)
        if set_fields:
            row.update(set_fields)
        bucket[key] = copy.deepcopy(row)

    @staticmethod
    def _matches(row: dict[str, Any], where: dict[str, Any]) -> bool:
        """Equality only. `{f: v}` or `{f: [v1, v2]}`. No operators => nothing to inject."""
        for field, expected in where.items():
            actual = row.get(field)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    async def find(
        self,
        collection: str,
        where: dict[str, Any],
        *,
        limit: int = 100,
        after: str | None = None,
        sort: tuple[str, str] = ("created", "desc"),
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not where:
            raise ConfigError("find() requires a non-empty `where` - use scan_all() to read all")
        field, direction = sort
        if direction not in SORT_DIRECTIONS:
            raise ConfigError(f"sort direction must be one of {SORT_DIRECTIONS}: {direction!r}")
        limit = max(1, int(limit))

        rows = [r for r in self._data.get(collection, {}).values() if self._matches(r, where)]
        descending = direction == "desc"
        rows.sort(key=lambda r: (_cursor._key(r.get(field)), str(r.get("id", ""))),
                  reverse=descending)

        position = _cursor.decode(after) if after else None
        if position is not None:
            rows = [r for r in rows
                    if _cursor.after(r.get(field), str(r.get("id", "")), position, descending)]

        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _cursor.encode(last.get(field), str(last.get("id", "")))
        return copy.deepcopy(page), next_cursor

    async def scan_all(self, collection: str, limit: int, after: str = "") -> list[dict[str, Any]]:
        """Every row, ordered by KEY (matching postgres) so a paged sweep is deterministic. `after`
        returns only keys strictly greater than it — a cursor that lets a sweeper page through the
        WHOLE collection over calls, instead of forever re-reading the same lowest-key prefix."""
        items = sorted(self._data.get(collection, {}).items())
        if after:
            items = [(k, v) for k, v in items if k > after]
        return copy.deepcopy([v for _k, v in items[: max(0, int(limit))]])

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self.scan_all(collection, limit)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
