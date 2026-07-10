"""ObjectStore contract — memory + local (both zero-infra)."""
from __future__ import annotations

import pytest

from app.adapters.object_store.local import LocalObjectStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.ports.errors import NotFound
from tests.contracts.util import run


@pytest.fixture(params=["memory", "local"])
def store(request, tmp_path):
    s = MemoryObjectStore() if request.param == "memory" else LocalObjectStore(str(tmp_path))
    yield s
    run(s.close())


def test_put_get_roundtrip(store):
    run(store.put("dir/file.txt", b"hello"))
    assert run(store.get("dir/file.txt")) == b"hello"


def test_exists_and_delete(store):
    run(store.put("a.bin", b"x"))
    assert run(store.exists("a.bin")) is True
    assert run(store.delete("a.bin")) is True
    assert run(store.exists("a.bin")) is False


def test_get_missing_raises_notfound(store):
    with pytest.raises(NotFound):
        run(store.get("missing.bin"))


def test_health(store):
    assert run(store.health()) is True
