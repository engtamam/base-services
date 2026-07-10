"""DataStore contract — every DataStore adapter must satisfy this."""
from __future__ import annotations

import os

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from tests.contracts.util import run

# The postgres adapter is held to the SAME contract, against a REAL server, whenever
# DATASTORE_TEST_DSN is set. Without it these params are skipped and the atomic primitives
# (`incr_if_below` / `cas`) would only ever be proven on the in-memory adapter — where an
# asyncio.Lock hides every race a real database has to serialise itself.
#
#   docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16.4-alpine
#   DATASTORE_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5432/postgres pytest tests/contracts
_PG_DSN = os.environ.get("DATASTORE_TEST_DSN", "")


def _memory():
    return MemoryDataStore()


def _postgres():
    from app.adapters.datastore.postgres import PostgresDataStore

    store = PostgresDataStore(_PG_DSN)

    async def _reset():
        pool = await store._ensure()
        await pool.execute("TRUNCATE kv_store")

    run(_reset())
    return store


_PARAMS = [pytest.param(_memory, id="memory")]
if _PG_DSN:
    _PARAMS.append(pytest.param(_postgres, id="postgres"))
else:
    _PARAMS.append(pytest.param(
        _postgres, id="postgres",
        marks=pytest.mark.skip(reason="set DATASTORE_TEST_DSN to run the postgres contract")))


@pytest.fixture(params=_PARAMS)
def store(request):
    s = request.param()
    yield s
    run(s.close())


def test_put_get_roundtrip(store):
    run(store.put("things", "a", {"n": 1}))
    assert run(store.get("things", "a")) == {"n": 1}


def test_get_missing_returns_none(store):
    assert run(store.get("things", "nope")) is None


def test_delete(store):
    run(store.put("things", "a", {"n": 1}))
    assert run(store.delete("things", "a")) is True
    assert run(store.delete("things", "a")) is False
    assert run(store.get("things", "a")) is None


def test_list_and_collection_isolation(store):
    run(store.put("c1", "a", {"n": 1}))
    run(store.put("c1", "b", {"n": 2}))
    run(store.put("c2", "a", {"n": 9}))
    assert len(run(store.list("c1"))) == 2
    assert len(run(store.list("c2"))) == 1


def test_returned_value_is_isolated_copy(store):
    run(store.put("c", "a", {"n": 1}))
    got = run(store.get("c", "a"))
    got["n"] = 999
    assert run(store.get("c", "a")) == {"n": 1}


def test_health(store):
    assert run(store.health()) is True


# ── cas: the primitive a wallet cannot be correct without ───────────────────
# `get` then `put` is a lost update: two spenders read the same balance and the second
# overwrites the first, giving away money. The compare and the swap must be one step.
def test_cas_creates_only_when_absent(store):
    assert run(store.cas("w", "t1", 0, {"funds": {"cash": 10}})) is True
    assert run(store.cas("w", "t1", 0, {"funds": {"cash": 99}})) is False   # already exists
    assert run(store.get("w", "t1"))["funds"] == {"cash": 10}


def test_cas_stamps_and_advances_the_version(store):
    run(store.cas("w", "t1", 0, {"funds": {}}))
    assert run(store.get("w", "t1"))["version"] == 1
    assert run(store.cas("w", "t1", 1, {"funds": {"cash": 5}})) is True
    assert run(store.get("w", "t1"))["version"] == 2


def test_cas_rejects_a_stale_version_and_changes_nothing(store):
    run(store.cas("w", "t1", 0, {"funds": {"cash": 10}}))
    assert run(store.cas("w", "t1", 0, {"funds": {"cash": 0}})) is False    # stale reader
    assert run(store.get("w", "t1"))["funds"] == {"cash": 10}


def test_cas_lets_exactly_one_of_many_concurrent_writers_win(store):
    import asyncio

    run(store.cas("w", "t1", 0, {"funds": {"cash": 100}}))

    async def race():
        return await asyncio.gather(*[store.cas("w", "t1", 1, {"funds": {"cash": i}})
                                      for i in range(50)])

    # `run` uses the session loop; a pooled adapter cannot survive a fresh asyncio.run().
    assert sum(1 for ok in run(race()) if ok) == 1
