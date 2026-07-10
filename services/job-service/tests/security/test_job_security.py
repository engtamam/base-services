"""Security: deny-by-default, internal-token gate, lease-token hidden, fail-fast drivers."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_deny_by_default(client):
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.post("/api/v1/jobs/schedules", json={"name": "x", "cron": "* * * * *"}).status_code == 401


def test_internal_endpoints_require_token(client):
    for path in ("/internal/jobs/enqueue", "/internal/jobs/claim", "/internal/jobs/ack",
                 "/internal/jobs/reclaim", "/internal/scheduler/tick"):
        assert client.post(path, json={}).status_code == 401


def test_public_job_view_hides_the_lease_token():
    from app.core.models import Job
    j = Job(id="j", lease_token="lt_secret")
    assert "lease_token" not in j.public()


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40, CLOCK_DRIVER="sundial"))
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40, CRON_PARSER_DRIVER="guess"))
