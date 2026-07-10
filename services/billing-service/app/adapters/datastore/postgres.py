"""PostgreSQL DataStore (asyncpg) — infra-gated, lazy import.

Generic JSONB key/value table so the template is usable out of the box; real
services replace this with their own schema/repository behind the same port.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.adapters.lazy import load
from app.adapters.datastore import cursor as _cursor
from app.ports.datastore import SORT_DIRECTIONS
from app.ports.errors import ConfigError

_TABLE = "kv_store"

# A `find` without an index is a sequential scan wearing a WHERE clause. These ship WITH the
# adapter, because an index the operator has to remember to create is an index that is not there
# the night it matters.
#   - the GIN index serves `value @> '{"tenant": "..."}'` containment (equality filters)
#   - the btree serves the keyset ORDER BY / cursor comparison
#
# The ordering index expression must match the ORDER BY expression EXACTLY, COALESCE and all.
# `(value->>'created')` does not match `COALESCE(value->>'created','')`, so Postgres cannot use
# it to order: it bitmap-scans every one of the tenant's rows and top-N sorts them. Measured on
# 100 000 rows / 20 tenants, one tenant's first page:
#     mismatched index  -> Bitmap Heap Scan, 5 000 rows read, Sort, 3.44 ms
#     matched index     -> Index Scan, no Sort,                     0.92 ms
# The first is O(that tenant's rows). Only the second is O(page).
_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS kv_value_gin ON {_TABLE} USING gin (value jsonb_path_ops)",
    f"CREATE INDEX IF NOT EXISTS kv_created_desc ON {_TABLE} "
    f"(collection, COALESCE(value->>'created', '') DESC, key DESC)",
)



class PostgresDataStore:
    driver = "postgres"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ConfigError("DATASTORE_DSN is required for the postgres driver")
        self._asyncpg = load("asyncpg", "postgres")
        self._dsn = dsn
        self._pool: Any = None
        self._loop: Any = None

    async def _ensure(self) -> Any:
        # A connection pool belongs to the event loop that created it. If we are called from a
        # different loop, the old pool's sockets are attached to a loop that may be closed and
        # every query raises "Event loop is closed". Terminate it and build a fresh one rather
        # than fail obscurely.
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._loop is not loop:
            self._pool.terminate()          # synchronous; safe to call from another loop
            self._pool = None
        if self._pool is None:
            self._pool = await self._asyncpg.create_pool(self._dsn)
            self._loop = loop
            async with self._pool.acquire() as con:
                await con.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                    "collection text NOT NULL, key text NOT NULL, "
                    "value jsonb NOT NULL, PRIMARY KEY (collection, key))"
                )
                for statement in _INDEXES:
                    await con.execute(statement)
        return self._pool

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        pool = await self._ensure()
        async with pool.acquire() as con:
            row = await con.fetchval(
                f"SELECT value FROM {_TABLE} WHERE collection=$1 AND key=$2",  # nosec B608 - _TABLE is a trusted constant; all values are parameterized
                collection, key,
            )
        return json.loads(row) if row is not None else None

    async def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        pool = await self._ensure()
        async with pool.acquire() as con:
            await con.execute(
                f"INSERT INTO {_TABLE} (collection, key, value) VALUES ($1,$2,$3) "  # nosec B608 - _TABLE is a trusted constant; all values are parameterized
                "ON CONFLICT (collection, key) DO UPDATE SET value=EXCLUDED.value",
                collection, key, json.dumps(value),
            )

    async def delete(self, collection: str, key: str) -> bool:
        pool = await self._ensure()
        async with pool.acquire() as con:
            res = await con.execute(
                f"DELETE FROM {_TABLE} WHERE collection=$1 AND key=$2", collection, key  # nosec B608 - _TABLE is a trusted constant; values parameterized
            )
        return res.endswith("1")

    async def find(
        self,
        collection: str,
        where: dict[str, Any],
        *,
        limit: int = 100,
        after: str | None = None,
        sort: tuple[str, str] = ("created", "desc"),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Filter, order and page IN THE DATABASE. See the port for why.

        The equality filter becomes a JSONB containment (`@>`), which the GIN index serves.
        Membership (`{field: [a, b]}`) becomes an OR of containments — still indexed.
        The cursor becomes a tuple comparison on `(sort_field, key)`, which is exactly what the
        btree index is ordered by, so "the next page" is a seek, not a scan.
        """
        if not where:
            raise ConfigError("find() requires a non-empty `where` — use scan_all() to read all")
        field, direction = sort
        if direction not in SORT_DIRECTIONS:
            raise ConfigError(f"sort direction must be one of {SORT_DIRECTIONS}: {direction!r}")
        limit = max(1, int(limit))
        descending = direction == "desc"

        args: list[Any] = [collection]
        clauses: list[str] = ["collection = $1"]

        for name, expected in where.items():
            if isinstance(expected, (list, tuple, set)):
                ors = []
                for item in expected:
                    args.append(json.dumps({name: item}))
                    ors.append(f"value @> ${len(args)}::jsonb")
                clauses.append("(" + " OR ".join(ors) + ")" if ors else "FALSE")
            else:
                args.append(json.dumps({name: expected}))
                clauses.append(f"value @> ${len(args)}::jsonb")

        position = _cursor.decode(after) if after else None
        if position is not None:
            last_value, last_id = position
            args.append("" if last_value is None else str(last_value))
            value_param = len(args)
            args.append(str(last_id))
            id_param = len(args)
            comparison = "<" if descending else ">"
            # Tuple comparison: the id tie-break keeps the order total, so a page boundary
            # cannot fall between two rows sharing a `created` value.
            clauses.append(
                f"(COALESCE(value->>'{field}', ''), key) {comparison} (${value_param}, ${id_param})"
            )

        args.append(limit + 1)          # one extra row tells us whether another page exists
        order = "DESC" if descending else "ASC"
        sql = (
            f"SELECT key, value FROM {_TABLE} WHERE " + " AND ".join(clauses) +  # nosec B608 - _TABLE and the field name are trusted; all VALUES are parameterized
            f" ORDER BY COALESCE(value->>'{field}', '') {order}, key {order} LIMIT ${len(args)}"
        )

        pool = await self._ensure()
        async with pool.acquire() as con:
            rows = await con.fetch(sql, *args)

        page = [json.loads(r["value"]) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _cursor.encode(last.get(field), str(last.get("id", "")))
        return page, next_cursor

    async def scan_all(self, collection: str, limit: int) -> list[dict[str, Any]]:
        """Every row. Sweepers and migrations only — see the port.

        Ordered by key, so a truncated read is at least DETERMINISTIC. The old `list()` had no
        ORDER BY: past the limit it returned whatever the heap yielded, and a tenant could see
        an arbitrary subset of its own rows.
        """
        pool = await self._ensure()
        async with pool.acquire() as con:
            rows = await con.fetch(
                f"SELECT value FROM {_TABLE} WHERE collection=$1 ORDER BY key LIMIT $2",  # nosec B608 - _TABLE is a trusted constant; values parameterized
                collection, max(0, int(limit)),
            )
        return [json.loads(r["value"]) for r in rows]

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self.scan_all(collection, limit)


    async def incr_if_below(self, collection: str, key: str, delta: int,
                            limit: int | None) -> tuple[bool, int]:
        """Check-and-increment decided by the DATABASE, not by this process.

        Two statements, and the second one is where correctness lives:
          1. Ensure the counter row exists at 0. Idempotent; `DO NOTHING` on conflict.
             (Seeding it with `delta` instead would let the very first call skip the bound.)
          2. `UPDATE … WHERE current + delta <= limit RETURNING n`. Concurrent writers
             serialise on the row, so exactly one of two racing callers can take the last
             unit. If the WHERE fails, nothing is written and no row comes back.
        """
        pool = await self._ensure()
        d = int(delta)
        async with pool.acquire() as con:
            await con.execute(
                f"INSERT INTO {_TABLE} (collection, key, value) VALUES ($1,$2,'{{\"n\": 0}}'::jsonb) "  # nosec B608 - _TABLE is a trusted constant; values parameterized
                "ON CONFLICT (collection, key) DO NOTHING",
                collection, key,
            )
            cur = f"COALESCE((value->>'n')::bigint, 0)"
            if d < 0:
                # A refund always applies, clamped at 0: giving a unit back must never be
                # refused because the limit was lowered in the meantime.
                new_n = f"GREATEST({cur} + ({d}), 0)"
                where = ""
                args: list[Any] = [collection, key]
            elif limit is None:
                new_n = f"{cur} + {d}"
                where = ""
                args = [collection, key]
            else:
                new_n = f"{cur} + {d}"
                where = f" AND ({cur} + {d}) <= $3"
                args = [collection, key, int(limit)]

            row = await con.fetchval(
                f"UPDATE {_TABLE} SET value = jsonb_set(value, '{{n}}', to_jsonb({new_n})) "  # nosec B608 - _TABLE/new_n/where are built from a trusted int and constants; user values are parameterized
                f"WHERE collection=$1 AND key=$2{where} "
                "RETURNING (value->>'n')::bigint",
                *args,
            )
        if row is None:
            current = await self.get(collection, key)     # bound rejected it; report unchanged
            return False, int((current or {}).get("n", 0))
        return True, int(row)

    async def health(self) -> bool:
        try:
            pool = await self._ensure()
            async with pool.acquire() as con:
                await con.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is None:
            return
        # `close()` gracefully drains connections on the pool's OWN loop. Called from another
        # loop it would await a future that can never resolve, so terminate instead.
        try:
            if self._loop is asyncio.get_running_loop():
                await self._pool.close()
            else:
                self._pool.terminate()
        except RuntimeError:                 # no running loop at all
            self._pool.terminate()
        finally:
            self._pool = None
            self._loop = None
