"""The operator surface: see the flow catalogue, see a customer's provisioning run, resume stuck
cleanup. The gating is the load-bearing part — these are onboarding.manage / platform.onboarding
only, never a plain user capability."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ISS, AUD = "iss", "aud"


@pytest.fixture(scope="module")
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(env_guard) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER=ISS, TOKEN_AUDIENCE=AUD,
                      RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(*caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


OP = "onboarding.manage"


def test_the_operator_reads_need_the_manage_capability(client):
    """A plain user capability must not reach the operator surface."""
    for method, url in [("get", "/api/v1/onboarding/admin/flows"),
                        ("get", "/api/v1/onboarding/admin/provisioning/sub_1"),
                        ("post", "/api/v1/onboarding/admin/resume/sub_1")]:
        assert getattr(client, method)(url, headers=_auth("read")).status_code == 403, url
        assert getattr(client, method)(url).status_code == 401, url


def test_the_flow_catalogue_is_readable(client):
    r = client.get("/api/v1/onboarding/admin/flows", headers=_auth(OP))
    assert r.status_code == 200 and "flows" in r.json()


def test_a_subject_with_no_provisioning_run_reads_as_no_outstanding_work(client):
    r = client.get("/api/v1/onboarding/admin/provisioning/never_provisioned", headers=_auth(OP))
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is False and body["unfinished"] == []


def test_resuming_with_nothing_outstanding_is_a_clean_noop(client):
    r = client.post("/api/v1/onboarding/admin/resume/never_provisioned", headers=_auth(OP))
    assert r.status_code == 200 and r.json()["resumed"] is False
