"""DataStore port — generic record persistence (repository backing).

`find` exists because without it a tenant filter can only be written in Python, and a repository
that filters in Python must first read everything. `FileRepository.list()` pulled up to a
million rows across every tenant, filtered, sorted, and returned a hundred. Three consequences,
in ascending order of seriousness:

  * one tenant's request costs the size of everyone's data;
  * other tenants' rows cross the process boundary for no reason;
  * **past the cap the answer is silently wrong.** `LIMIT` without `ORDER BY` returns whatever
    the storage engine yields first, so a tenant can see an arbitrary subset of its own files —
    or none — and nothing errors.

`find` pushes the filter down: a comprehension in the memory adapter, indexed SQL in postgres.

`scan_all` is what `list` used to be. It is named to be uncomfortable: it reads a whole
collection, and belongs only in a sweeper or a migration, never on a request path.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Ordering directions a caller may ask for. Anything else is a typo, not a feature.
SORT_DIRECTIONS = ("asc", "desc")


@runtime_checkable
class DataStore(Protocol):
    driver: str

    async def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None: ...

    async def delete(self, collection: str, key: str) -> bool: ...

    async def incr(self, collection: str, key: str, delta: int) -> int:
        """Add `delta` to an integer counter and return the NEW value — ONE indivisible step.

        A ref-count built from get()+put() is a read-modify-write race: two concurrent uploads of
        the same content both read `n`, both write `n+1`, and one increment is lost — so the count
        undershoots and the blob is deleted while an owner still points at it. This op moves the
        add inside the store, where concurrent callers serialise on the row. The counter never goes
        below zero (a decrement past 0 clamps), and the row is created at `delta` if absent.
        """
        ...

    async def del_if_zero(self, collection: str, key: str) -> bool:
        """Delete the counter row IFF it is exactly zero. Return True only if THIS call removed it.

        This is what makes deleting the blob safe. The deleter decrements to 0, then calls this: if
        a concurrent uploader has already bumped the count back above 0, it returns False and the
        blob is kept. Only the caller that atomically removes the zero row may delete the blob —
        and because upload writes the blob BEFORE incrementing, a re-upload restores the bytes. No
        owner ever loses referenced bytes.
        """
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
        not a query language: there are no operators, so there is nothing to inject and no
        expression to evaluate. Same stance as every other DSL in this repo.

        `where` must be non-empty. `find(collection, {})` would be a cross-tenant leak one typo
        away from a repository that meant to scope by tenant.

        `after` is an opaque KEYSET cursor, never an offset. An offset is O(offset) and unstable:
        a row inserted between two pages makes the second page silently skip one.

        `sort` is (field, "asc"|"desc"). The order is made total by an `id` tie-break, or rows
        sharing a sort value straddle a page boundary and are dropped.
        """
        ...

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """EVERY row in a collection. A sweeper or a migration — never a request path.

        Deliberately has no default limit: how much of a table to read into memory is a
        decision, not something to inherit.
        """
        ...

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        """DEPRECATED alias of `scan_all`. Kept so existing sweepers keep working.

        It is the wrong tool for anything a user waits on. Use `find`.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
