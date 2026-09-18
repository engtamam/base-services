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

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """Compare-and-set on the row's `version` (atomic on the loop). Writes `value` only if the
        stored version equals `expected_version`, stamping version = expected+1. expected_version=0 is
        insert-if-absent (ANY existing row fails, versionless included) — matching the postgres adapter
        (INSERT ... ON CONFLICT DO NOTHING). A row created by cas therefore always carries a version and
        can be transitioned; a row written by a plain put() cannot be cas-updated with expected 0, which
        is exactly the divergence that once 401'd every refresh in identity-service — do not reintroduce
        it. This is the atomic unique-claim + single-winner-transition primitive PaymentRequest relies
        on for its once-only terminal completion."""
        bucket = self._data.setdefault(collection, {})
        current = bucket.get(key)
        if int(expected_version) == 0:
            if current is not None:
                return False
            stored = copy.deepcopy(value)
            stored["version"] = 1
            bucket[key] = stored
            return True
        current_version = int(current.get("version", 0)) if current else 0
        if current_version != int(expected_version):
            return False
        stored = copy.deepcopy(value)
        stored["version"] = current_version + 1
        bucket[key] = stored
        return True

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

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """Every row. Sweepers and migrations only - see the port."""
        rows = list(self._data.get(collection, {}).values())
        return copy.deepcopy(rows[: max(0, int(limit))])

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self.scan_all(collection, limit)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
