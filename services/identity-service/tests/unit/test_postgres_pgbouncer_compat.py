"""The postgres adapter must be PgBouncer-transaction-mode SAFE by default.

In transaction pooling a server connection is reassigned per transaction, so asyncpg's prepared
statements (cached across calls) break — a statement prepared on one backend is gone on the next.
Passing statement_cache_size=0 to create_pool is the documented fix. This asserts the setting is
wired through to the pool (default 0), so a copy-paste can't quietly drop it and reintroduce the
"prepared statement does not exist" failures under load behind PgBouncer.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.postgres import PostgresDataStore


def test_statement_cache_size_defaults_to_zero_and_reaches_create_pool():
    p = PostgresDataStore("postgresql://x/y")          # no arg → default
    assert p._statement_cache_size == 0

    captured: dict = {}

    class _FakeAsyncpg:
        async def create_pool(self, dsn, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop")                 # short-circuit before the CREATE TABLE

    p._asyncpg = _FakeAsyncpg()
    with pytest.raises(RuntimeError):
        asyncio.run(p._ensure())
    assert captured.get("statement_cache_size") == 0, (
        "statement_cache_size=0 must reach asyncpg.create_pool, or PgBouncer transaction mode breaks")


def test_a_direct_postgres_deployment_may_re_enable_the_cache():
    p = PostgresDataStore("postgresql://x/y", statement_cache_size=100)
    assert p._statement_cache_size == 100              # tunable for a non-PgBouncer deployment
