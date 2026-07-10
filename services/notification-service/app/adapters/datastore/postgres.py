"""PostgreSQL DataStore (asyncpg) — infra-gated, lazy import.

Generic JSONB key/value table so the template is usable out of the box; real
services replace this with their own schema/repository behind the same port.
"""
from __future__ import annotations

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

    async def _ensure(self) -> Any:
        if self._pool is None:
            self._pool = await self._asyncpg.create_pool(self._dsn)
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

    async def health(self) -> bool:
        try:
            pool = await self._ensure()
            async with pool.acquire() as con:
                await con.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
