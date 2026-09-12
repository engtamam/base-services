"""DataStore port — generic record persistence (repository backing)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Ordering directions a caller may ask for. Anything else is a typo.
SORT_DIRECTIONS = ("asc", "desc")


@runtime_checkable
class DataStore(Protocol):
    driver: str

    async def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    async def delete(self, collection: str, key: str) -> bool: ...

    async def find(
        self,
        collection: str,
        where: dict[str, Any],
        *,
        limit: int = 100,
        after: str | None = None,
        sort: tuple[str, str] = ("created", "desc"),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Rows matching `where`, ordered, paged. Returns (rows, next_cursor).

        `where` is EQUALITY ONLY: `{field: value}`, or `{field: [v1, v2]}` for membership. It is
        not a query language: there are no operators, so there is nothing to inject.

        `where` must be non-empty. `find(collection, {})` would be a cross-tenant leak one typo
        away from a repository that meant to scope by tenant.

        `after` is an opaque KEYSET cursor, never an offset. An offset is O(offset) and unstable:
        a row inserted between two pages makes the second page silently skip one.

        `sort` is (field, "asc"|"desc"), made total by an `id` tie-break.
        """
        ...

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """Compare-and-set: write `value` only if the stored row's `version` equals
        `expected_version`, returning True on success and False on a version mismatch (a lost race).

        The stored row's `version` is stamped by this method (expected_version + 1); callers never
        set it. `expected_version=0` means "insert only if absent" — so a row under optimistic locking
        must seed version>=1 the first time it is written, or a second insert-if-absent would also see
        version 0 and both would think they won.
        """
        ...

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """EVERY row in a collection. A sweeper or a migration — never a request path."""
        ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        """DEPRECATED alias of `scan_all`. Use `find`."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
