"""PostgreSQL DataStore (asyncpg) — infra-gated, lazy import.

Generic JSONB key/value table so the template is usable out of the box; real
services replace this with their own schema/repository behind the same port.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError

_TABLE = "kv_store"


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

    async def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        pool = await self._ensure()
        async with pool.acquire() as con:
            rows = await con.fetch(
                f"SELECT value FROM {_TABLE} WHERE collection=$1 LIMIT $2",  # nosec B608 - _TABLE is a trusted constant; values parameterized
                collection, max(0, limit),
            )
        return [json.loads(r["value"]) for r in rows]

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
