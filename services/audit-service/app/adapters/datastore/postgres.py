"""PostgreSQL DataStore (asyncpg) — infra-gated, lazy import.

Generic JSONB key/value table so the template is usable out of the box; real
services replace this with their own schema/repository behind the same port.
"""
from __future__ import annotations

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
    f"(collection, COALESCE(value->'created', 'null'::jsonb) DESC, key DESC)",
    # created_at is the second common ordering field (membership/person rows sort by it); index it too
    # so those keyset scans are O(page), not O(tenant-rows). Uses jsonb ordering (-> not ->>) so a
    # NUMERIC timestamp sorts numerically, matching the memory adapter — text ordering placed '100'
    # before '9' and mis-ordered every 'oldest-first' priority view.
    f"CREATE INDEX IF NOT EXISTS kv_created_at_desc ON {_TABLE} "
    f"(collection, COALESCE(value->'created_at', 'null'::jsonb) DESC, key DESC)",
    # audit events order by '_seq_key' (a zero-padded per-partition sequence), not 'created'. Without a
    # matching index every audit query/export/verify GIN-matched the partition then Sorted the WHOLE
    # partition (O(all events) with the default single 'global' partition). Index it so paging seeks.
    f"CREATE INDEX IF NOT EXISTS kv_seq_key_desc ON {_TABLE} "
    f"(collection, COALESCE(value->'_seq_key', 'null'::jsonb) DESC, key DESC)",
)



# The health probe waits at most this long for a slot. It is deliberately SHORT — shorter than the
# readyz per-check budget — so an exhausted pool is reported as "pool exhausted" here rather
# than being cancelled by the outer readyz timeout and mislabelled "timeout". Real queries
# still wait the full pool timeout; only the diagnosis is impatient.
_PROBE_ACQUIRE_SECONDS = 2.0


def _is_capacity_error(exc: BaseException) -> bool:
    """True when postgres is UP but FULL — NOT down. Two shapes: the SERVER's max_connections
    ceiling (asyncpg's TooManyConnectionsError, SQLSTATE 53300, "too many clients"), and this pool's
    own slots all busy (the acquire timed out). A full database is a capacity problem, and it reads
    completely differently to whoever is paged than an unreachable one."""
    import asyncio as _asyncio
    if isinstance(exc, (_asyncio.TimeoutError, TimeoutError)):
        return True
    if type(exc).__name__ in ("TooManyConnectionsError", "PoolTimeoutError"):
        return True
    if str(getattr(exc, "sqlstate", "") or "") == "53300":   # too_many_connections
        return True
    m = str(exc).lower()
    return "too many clients" in m or "too many connections" in m


class PostgresDataStore:
    driver = "postgres"

    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 5,
                 pool_timeout: float = 10.0, pool_max_inactive_lifetime: float = 300.0,
                 statement_cache_size: int = 0) -> None:
        if not dsn:
            raise ConfigError("DATASTORE_DSN is required for the postgres driver")
        self._asyncpg = load("asyncpg", "postgres")
        self._dsn = dsn
        self._pool: Any = None
        # Pool sizing (see config). min is LAZY and small (1): asyncpg's default is min_size=10, an
        # EAGER floor a pool climbs to on first use and then HOLDS for the process lifetime — so 24
        # services silently consume 240 backend connections against a single postgres whose stock
        # ceiling is 100, and the fleet deadlocks on an ordinary request once enough distinct
        # services have been touched. min=1 means a pod needs ONE slot to be Ready, not ten, and an
        # idle service costs one connection, not ten.
        self._pool_min = max(0, int(pool_min))
        self._pool_max = max(self._pool_min, int(pool_max), 1)
        self._pool_timeout = float(pool_timeout)
        self._pool_max_inactive = float(pool_max_inactive_lifetime)
        # PgBouncer transaction mode drops per-session state; disabling asyncpg's prepared-
        # statement cache (0) is the documented fix. Default 0 = fleet is PgBouncer-safe.
        self._statement_cache_size = max(0, int(statement_cache_size))

    async def _ensure(self) -> Any:
        if self._pool is None:
            self._pool = await self._asyncpg.create_pool(
                self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
                timeout=self._pool_timeout,                        # wait this long for a slot, then fail cleanly
                max_inactive_connection_lifetime=self._pool_max_inactive,
                statement_cache_size=self._statement_cache_size,  # return idle connections to the server
            )
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

    async def put_if_absent(self, collection: str, key: str, value: dict[str, Any]) -> bool:
        # ON CONFLICT DO NOTHING is the atomic insert-if-absent: the primary key (collection, key)
        # lets exactly one concurrent inserter win. `execute` returns "INSERT 0 1" when a row was
        # created and "INSERT 0 0" when the key already existed — that is the reservation verdict.
        pool = await self._ensure()
        async with pool.acquire() as con:
            res = await con.execute(
                f"INSERT INTO {_TABLE} (collection, key, value) VALUES ($1,$2,$3) "  # nosec B608 - _TABLE is a trusted constant; all values are parameterized
                "ON CONFLICT (collection, key) DO NOTHING",
                collection, key, json.dumps(value),
            )
        return res.strip().endswith("1")

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
            args.append(json.dumps(last_value))   # jsonb-compared below (None -> JSON null)
            value_param = len(args)
            args.append(str(last_id))
            id_param = len(args)
            comparison = "<" if descending else ">"
            # Tuple comparison: the id tie-break keeps the order total, so a page boundary
            # cannot fall between two rows sharing a `created` value.
            clauses.append(
                f"(COALESCE(value->'{field}', 'null'::jsonb), key) {comparison} (${value_param}::jsonb, ${id_param})"
            )

        args.append(limit + 1)          # one extra row tells us whether another page exists
        order = "DESC" if descending else "ASC"
        sql = (
            f"SELECT key, value FROM {_TABLE} WHERE " + " AND ".join(clauses) +  # nosec B608 - _TABLE and the field name are trusted; all VALUES are parameterized
            f" ORDER BY COALESCE(value->'{field}', 'null'::jsonb) {order}, key {order} LIMIT ${len(args)}"
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


    async def health_state(self) -> str:
        """Readiness with a REASON: 'ok', 'pool exhausted (max_connections reached)', or 'down'.

        The exhausted case is the one that read as 'down' and sent an operator to a database that
        was UP and healthy — it was FULL. Postgres answers `too many clients` (SQLSTATE 53300) at
        the server ceiling; a pool whose own slots are all busy times the acquire out. Both mean
        full, not unreachable, and naming it is a one-line diagnosis instead of an hour spent on the
        wrong thing. The acquire is bounded by the pool timeout so a full pool cannot hang the probe.
        """
        try:
            pool = await self._ensure()
            async with pool.acquire(timeout=min(self._pool_timeout, _PROBE_ACQUIRE_SECONDS)) as con:
                await con.execute("SELECT 1")
            return "ok"
        except Exception as exc:  # noqa: BLE001 — a probe CLASSIFIES failures, it does not raise
            return "pool exhausted (max_connections reached)" if _is_capacity_error(exc) else "down"

    async def health(self) -> bool:
        # Kept for any caller that wants a plain boolean; readiness uses health_state for the reason.
        return await self.health_state() == "ok"

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
