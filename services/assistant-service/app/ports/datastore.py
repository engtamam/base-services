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

    async def add_fields(self, collection: str, key: str, deltas: dict[str, int],
                         set_fields: dict[str, Any] | None = None) -> None:
        """ATOMICALLY add integer `deltas` to a row's numeric fields (creating the row if absent),
        and overwrite `set_fields` verbatim. This is one indivisible read-modify-write, so two
        concurrent callers each incrementing the same counter never lose an increment — the failure a
        plain get()+put() has (both read N, both write N+1). Used by additive ledgers (usage/cost
        metering) where a lost increment is silently under-billed money. Missing counters start at 0.
        """
        ...

    async def scan_all(self, collection: str, limit: int, after: str = "") -> list[dict[str, Any]]:
        """EVERY row in a collection, ordered by key. A sweeper or a migration — never a request path.
        `after` pages past a key so a sweep can cover the whole collection over successive calls."""
        ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        """DEPRECATED alias of `scan_all`. Use `find`."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
