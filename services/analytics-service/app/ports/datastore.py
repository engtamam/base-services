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

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """Compare-and-set on `value["version"]`. True if written, False if raced. expected_version=0
        is an atomic insert-if-absent."""
        ...

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

    async def get_many(self, collection: str, keys: list[str]) -> list[dict[str, Any]]:
        """The keyed hot path: fetch exactly `keys` in ONE round trip, skipping absent rows.

        Replaces a per-key `get()` loop, which on a wide query (e.g. ~9600 hourly buckets) was
        ~9600 sequential point reads that pinned a pool slot for the whole request. Order of the
        returned rows is unspecified — callers key off the row contents, not position."""
        ...

    async def range_scan(self, collection: str, lo: str, hi: str,
                         limit: int) -> list[dict[str, Any]]:
        """Rows whose KEY is in [lo, hi), ordered by key — a single indexed range read served by
        the primary-key btree, NOT a whole-collection scan+filter.

        This is the group-by hot path: a rollup key is `tenant|metric|granularity|bucket|dims`
        (lexicographically time-ordered within a granularity), so one range read over a bounded
        [lo, hi) returns every dimension cell in a query's bucket range and CANNOT be evicted by
        another tenant's volume the way a capped `scan_all` could. `lo`/`hi` must already pin the
        tenant prefix — an unbounded range is a cross-tenant read one typo away."""
        ...

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """EVERY row in a collection. A sweeper or a migration — never a request path."""
        ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        """DEPRECATED alias of `scan_all`. Use `find`."""
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
