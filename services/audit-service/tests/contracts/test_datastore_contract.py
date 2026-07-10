"""DataStore contract — every DataStore adapter must satisfy this."""
from __future__ import annotations

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from tests.contracts.util import run


@pytest.fixture(params=[MemoryDataStore])
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
