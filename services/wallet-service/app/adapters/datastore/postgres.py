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

    async def cas(self, collection: str, key: str, expected_version: int,
                  value: dict[str, Any]) -> bool:
        """Compare-and-swap decided by the DATABASE, not by this process.

        `expected_version=0` is an INSERT that must not clobber an existing row, so it uses
        `ON CONFLICT DO NOTHING` rather than an upsert. Otherwise it is a single UPDATE whose
        WHERE clause pins the version: concurrent writers serialise on the row and the loser
        changes nothing.
        """
        pool = await self._ensure()
        expected = int(expected_version)
        stored = dict(value)
        stored["version"] = expected + 1
        payload = json.dumps(stored)
        async with pool.acquire() as con:
            if expected == 0:
                res = await con.execute(
                    f"INSERT INTO {_TABLE} (collection, key, value) VALUES ($1,$2,$3) "  # nosec B608 - _TABLE is a trusted constant; values parameterized
                    "ON CONFLICT (collection, key) DO NOTHING",
                    collection, key, payload,
                )
                return res.endswith("1")
            res = await con.execute(
                f"UPDATE {_TABLE} SET value=$3 WHERE collection=$1 AND key=$2 "  # nosec B608 - _TABLE is a trusted constant; values parameterized
                "AND COALESCE((value->>'version')::bigint, 0) = $4",
                collection, key, payload, expected,
            )
        return res.endswith("1")

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
