"""RateLimiter contract."""
from __future__ import annotations

import os

import pytest

from app.adapters.rate_limiter.memory import MemoryRateLimiter
from tests.contracts.util import run

# The redis adapter is held to the SAME contract against a REAL server whenever REDIS_TEST_URL is set,
# and SKIPS VISIBLY otherwise (mirrors the datastore contract's DATASTORE_TEST_DSN):
#   docker run -d -p 6379:6379 redis:7-alpine
#   REDIS_TEST_URL=redis://127.0.0.1:6379/0 pytest tests/contracts
_REDIS_URL = os.environ.get("REDIS_TEST_URL", "")


def _memory():
    return MemoryRateLimiter(max_requests=3, window_seconds=60)


def _redis():
    from app.adapters.rate_limiter.redis import RedisRateLimiter
    return RedisRateLimiter(_REDIS_URL, 3, 60)


_PARAMS = [pytest.param(_memory, id="memory")]
_PARAMS.append(pytest.param(_redis, id="redis") if _REDIS_URL else pytest.param(
    _redis, id="redis", marks=pytest.mark.skip(reason="set REDIS_TEST_URL to run the redis contract")))


@pytest.fixture(params=_PARAMS)
def limiter(request):
    limiter = request.param()
    yield limiter
    run(limiter.close())


def test_allows_up_to_max_then_blocks(limiter):
    assert [run(limiter.allow("k")) for _ in range(4)] == [True, True, True, False]


def test_per_key_independent(limiter):
    for _ in range(3):
        run(limiter.allow("a"))
    assert run(limiter.allow("a")) is False
    assert run(limiter.allow("b")) is True  # different key, own budget


def test_reset_restores(limiter):
    for _ in range(3):
        run(limiter.allow("k"))
    run(limiter.reset("k"))
    assert run(limiter.allow("k")) is True
