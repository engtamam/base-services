"""RateLimiter contract."""
from __future__ import annotations

import pytest

from app.adapters.rate_limiter.memory import MemoryRateLimiter
from tests.contracts.util import run


@pytest.fixture
def limiter():
    limiter = MemoryRateLimiter(max_requests=3, window_seconds=60)
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
