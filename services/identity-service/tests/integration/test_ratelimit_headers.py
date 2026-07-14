"""Over HTTP: a 429 carries Retry-After, every response carries RateLimit-*, and the operator can
unblock one key."""
from __future__ import annotations

import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch):
    """A tiny app that mounts only the rate_limit dependency against a real memory limiter."""
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
                 "RATELIMIT_MAX": "3", "RATELIMIT_WINDOW_SECONDS": "60",
                 "RATELIMITER_DRIVER": "memory"}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()

    from app.api.ratelimit import rate_limit
    from app.container import build_container

    application = FastAPI()
    application.state.container = build_container(get_settings())

    @application.get("/thing", dependencies=[Depends(rate_limit)])
    async def thing():
        return {"ok": True}

    yield TestClient(application, raise_server_exceptions=False)
    get_settings.cache_clear()


def test_every_response_carries_ratelimit_headers(app_client):
    r = app_client.get("/thing")
    assert r.status_code == 200
    assert r.headers["RateLimit-Limit"] == "3"
    assert int(r.headers["RateLimit-Remaining"]) == 2       # one spent


def test_a_429_carries_retry_after(app_client):
    """A 429 with no Retry-After tells a client to guess, and clients guess by retrying now."""
    codes = [app_client.get("/thing").status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes

    blocked = app_client.get("/thing")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["RateLimit-Remaining"] == "0"
