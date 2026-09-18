"""Cache contract."""
from __future__ import annotations

import os

import pytest

from app.adapters.cache.memory import MemoryCache
from tests.contracts.util import run

# The redis adapter is held to the SAME contract, against a REAL server, whenever REDIS_TEST_URL is
# set — otherwise the contract only ever proves the in-memory adapter agrees with itself (its dict
# hides the eviction, TTL and connection semantics a real cache has). It SKIPS VISIBLY when no server
# is reachable, so the option is always on the record. Mirrors the datastore contract's
# DATASTORE_TEST_DSN. Start one with:
#   docker run -d -p 6379:6379 redis:7-alpine
#   REDIS_TEST_URL=redis://127.0.0.1:6379/0 pytest tests/contracts
_REDIS_URL = os.environ.get("REDIS_TEST_URL", "")


def _memory():
    return MemoryCache()


def _redis():
    from app.adapters.cache.redis import RedisCache
    return RedisCache(_REDIS_URL)


_PARAMS = [pytest.param(_memory, id="memory")]
_PARAMS.append(pytest.param(_redis, id="redis") if _REDIS_URL else pytest.param(
    _redis, id="redis", marks=pytest.mark.skip(reason="set REDIS_TEST_URL to run the redis contract")))


@pytest.fixture(params=_PARAMS)
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
