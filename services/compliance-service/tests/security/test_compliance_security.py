"""Security: deny-by-default, internal token, constant-time verify, no token leakage."""
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
    assert client.get("/api/v1/compliance/dsar").status_code == 401
    assert client.post("/api/v1/compliance/legal-holds", json={"subject": "u", "reason": "r"}).status_code == 401


def test_internal_endpoints_require_token(client):
    assert client.get("/internal/compliance/consent-check?tenant=t&subject=s&purpose=p").status_code == 401
    assert client.post("/internal/compliance/dsar/x/target-ack",
                       json={"service": "a", "category": "b"}).status_code == 401


def test_verification_uses_constant_time_compare():
    import inspect
    from app.core import dsar_service
    src = inspect.getsource(dsar_service)
    assert "compare_digest" in src and "==" not in src.split("compare_digest")[0][-80:]


def test_dsar_public_view_never_leaks_the_token():
    from app.core.models import Dsar
    d = Dsar(id="d", tenant="t", subject="s", verification_token="super-secret")
    assert "super-secret" not in str(d.public())


def test_null_orchestrator_reports_not_dispatched():
    import asyncio
    from app.adapters.orchestrator.null import NullOrchestrator
    ok = asyncio.run(NullOrchestrator().dispatch(request_id="r", action="erasure",
                                                 tenant="t", subject="s", target={}))
    assert ok is False       # never claim work was sent when it wasn't


def test_unconfigured_job_orchestrator_does_not_dispatch():
    import asyncio
    from app.adapters.orchestrator.job_http import JobHttpOrchestrator
    ok = asyncio.run(JobHttpOrchestrator("", "").dispatch(request_id="r", action="erasure",
                                                          tenant="t", subject="s", target={}))
    assert ok is False


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 ORCHESTRATOR_DRIVER="telepathy"))
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 CLOCK_DRIVER="sundial"))
