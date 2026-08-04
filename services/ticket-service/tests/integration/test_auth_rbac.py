"""Real auth + capability RBAC + rate limiting on /api/v1."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ISS = "iss"
AUD = "aud"


def _make_app(**env_over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256",
        "TOKEN_HS256_SECRET": SECRET,
        "TOKEN_ISSUER": ISS,
        "TOKEN_AUDIENCE": AUD,
        "RATELIMIT_MAX": "1000",
    }
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def auth_env():
    from app.config import get_settings

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(auth_env) -> TestClient:
    return _make_app()


def _token(caps=None, secret=SECRET, **over) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": "u1", "iss": ISS, "aud": AUD,
              "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ── Authentication ──────────────────────────────────────────────────────────
def test_no_bearer_401(client):
    assert client.get("/api/v1/whoami").status_code == 401


def test_valid_token_200(client):
    r = client.get("/api/v1/whoami", headers=_auth(_token(caps=["read"])))
    assert r.status_code == 200
    assert r.json()["subject"] == "u1"
    assert r.json()["caps"] == ["read"]


def test_invalid_token_401(client):
    assert client.get("/api/v1/whoami", headers=_auth("garbage")).status_code == 401


def test_expired_token_401(client):
    tok = _token(exp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
    assert client.get("/api/v1/whoami", headers=_auth(tok)).status_code == 401


def test_wrong_secret_401(client):
    assert client.get("/api/v1/whoami", headers=_auth(_token(secret="x" * 40))).status_code == 401


# ── Capability RBAC (deny-by-default) ───────────────────────────────────────
def test_admin_denied_without_cap(client):
    r = client.get("/api/v1/admin/ping", headers=_auth(_token(caps=["read"])))
    assert r.status_code == 403


def test_admin_denied_without_any_caps(client):
    # No caps claim at all -> no capabilities -> 403 (deny-by-default).
    r = client.get("/api/v1/admin/ping", headers=_auth(_token()))
    assert r.status_code == 403


def test_admin_allowed_with_cap(client):
    r = client.get("/api/v1/admin/ping", headers=_auth(_token(caps=["admin"])))
    assert r.status_code == 200


def test_role_claim_alone_does_not_grant(client):
    # A 'role: admin' claim without the 'admin' capability must NOT pass.
    r = client.get("/api/v1/admin/ping", headers=_auth(_token(role="admin")))
    assert r.status_code == 403


# ── Rate limiting ───────────────────────────────────────────────────────────
def test_rate_limit_429(auth_env):
    rl_client = _make_app(RATELIMIT_MAX="1")
    tok = _auth(_token(caps=["read"]))
    first = rl_client.get("/api/v1/whoami", headers=tok)
    second = rl_client.get("/api/v1/whoami", headers=tok)
    assert first.status_code == 200
    assert second.status_code == 429
