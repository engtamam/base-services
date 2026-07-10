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


# ── incr_if_below: the atomic primitive quota enforcement depends on ─────────
# get+put cannot enforce a limit: two callers read 24, both write 25, and the 26th
# seat is sold. Any adapter that cannot do this atomically must not back a quota.
def test_incr_if_below_applies_within_the_limit(store):
    assert run(store.incr_if_below("c", "k", 1, 10)) == (True, 1)
    assert run(store.incr_if_below("c", "k", 4, 10)) == (True, 5)


def test_incr_if_below_refuses_at_the_boundary_and_changes_nothing(store):
    run(store.incr_if_below("c", "k", 10, 10))
    assert run(store.incr_if_below("c", "k", 1, 10)) == (False, 10)
    assert run(store.get("c", "k")) == {"n": 10}


def test_incr_if_below_treats_none_as_unlimited(store):
    assert run(store.incr_if_below("c", "k", 10_000, None)) == (True, 10_000)


def test_incr_if_below_never_refuses_a_release_and_clamps_at_zero(store):
    run(store.incr_if_below("c", "k", 5, 10))
    assert run(store.incr_if_below("c", "k", -2, 10)) == (True, 3)
    assert run(store.incr_if_below("c", "k", -99, 10)) == (True, 0)


def test_incr_if_below_serialises_concurrent_callers(store):
    import asyncio

    async def race():
        return await asyncio.gather(*[store.incr_if_below("c", "seats", 1, 25)
                                      for _ in range(50)])

    results = run(race())          # the session loop: a pooled adapter needs its own loop
    assert sum(1 for ok, _ in results if ok) == 25
    assert run(store.get("c", "seats")) == {"n": 25}
