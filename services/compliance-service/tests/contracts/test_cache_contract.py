"""Cache contract."""
from __future__ import annotations

import pytest

from app.adapters.cache.memory import MemoryCache
from tests.contracts.util import run


@pytest.fixture(params=[MemoryCache])
def cache(request):
    c = request.param()
    yield c
    run(c.close())


def test_set_get(cache):
    run(cache.set("k", "v"))
    assert run(cache.get("k")) == "v"


def test_missing_returns_none(cache):
    assert run(cache.get("absent")) is None


def test_delete(cache):
    run(cache.set("k", "v"))
    assert run(cache.delete("k")) is True
    assert run(cache.get("k")) is None


def test_ttl_value_present_immediately(cache):
    run(cache.set("k", "v", ttl_seconds=100))
    assert run(cache.get("k")) == "v"


def test_health(cache):
    assert run(cache.health()) is True
