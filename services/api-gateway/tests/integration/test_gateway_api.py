"""Integration: proxy through the app with a fake forwarder (no network)."""
from __future__ import annotations

import datetime as dt
import json
import os

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.unit.test_gateway_core import FakeForwarder

SECRET = "h" * 40
ROUTES = json.dumps([
    {"prefix": "/echo", "upstream": "http://upstream:9000", "auth": "none", "strip_prefix": True},
    {"prefix": "/secure", "upstream": "http://upstream:9000", "auth": "required",
     "capabilities": ["reports.read"]},
])


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, GATEWAY_ROUTES=ROUTES,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.state.container.forwarder = FakeForwarder(status=200, body=b"UPSTREAM_OK")
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _fwd(client) -> FakeForwarder:
    return client.app.state.container.forwarder


def _token(caps=None, sub="u"):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_public_route_forwards(client):
    r = client.get("/echo/hello")
    assert r.status_code == 200 and r.text == "UPSTREAM_OK"
    assert _fwd(client).last["url"] == "http://upstream:9000/hello"
    assert _fwd(client).last["headers"]["X-Internal-Token"] == "t" * 40


def test_protected_route_requires_token(client):
    assert client.get("/secure/data").status_code == 401


def test_protected_route_needs_capability(client):
    r = client.get("/secure/data", headers={"Authorization": "Bearer " + _token(caps=["other"])})
    assert r.status_code == 403


def test_protected_route_ok_injects_identity(client):
    r = client.get("/secure/data",
                   headers={"Authorization": "Bearer " + _token(caps=["reports.read"], sub="alice")})
    assert r.status_code == 200
    assert _fwd(client).last["headers"]["X-User-Id"] == "alice"


def test_client_cannot_spoof_internal_token(client):
    client.get("/echo/x", headers={"X-Internal-Token": "forged", "X-User-Id": "attacker"})
    hdrs = _fwd(client).last["headers"]
    assert hdrs["X-Internal-Token"] == "t" * 40
    assert hdrs.get("X-User-Id") is None


def test_unconfigured_path_404(client):
    assert client.get("/nothing/here").status_code == 404
