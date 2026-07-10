"""Security properties: no enumeration, no secret leak, replay + expiry, RBAC."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.api.authz import require_any_capability, require_capability

SECRET = "h" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_ISSUER_DRIVER": "hs256", "TOKEN_SIGNING_SECRET": SECRET,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
        "RATELIMIT_MAX": "1000", "REGISTRATION_ENABLED": "true",
    }
    env.update(over)
    os.environ.update(env)
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


# ── No user enumeration (CWE-204) ───────────────────────────────────────────
def test_no_user_enumeration(client):
    client.post("/api/v1/auth/register", json={"username": "real", "password": "password1"})
    bad_user = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "password1"})
    bad_pass = client.post("/api/v1/auth/login", json={"username": "real", "password": "nope"})
    assert bad_user.status_code == bad_pass.status_code == 401
    # indistinguishable error code (request_id differs per request, by design)
    assert bad_user.json()["error"] == bad_pass.json()["error"]


# ── No secret leakage (CWE-200) ─────────────────────────────────────────────
def test_register_and_me_never_leak_hash(client):
    reg = client.post("/api/v1/auth/register", json={"username": "leaky", "password": "password1"})
    assert "password_hash" not in reg.text and "mfa_secret" not in reg.text
    tokens = client.post("/api/v1/auth/login", json={"username": "leaky", "password": "password1"}).json()
    me = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert "password_hash" not in me.text and "mfa_secret" not in me.text


# ── Expired access token rejected ───────────────────────────────────────────
def test_expired_access_token_rejected(client):
    now = dt.datetime.now(dt.timezone.utc)
    expired = jwt.encode(
        {"sub": "x", "type": "access", "iat": now - dt.timedelta(hours=2),
         "exp": now - dt.timedelta(hours=1)},
        SECRET, algorithm="HS256",
    )
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + expired}).status_code == 401


# ── Capability RBAC (deny-by-default) — dependency logic ────────────────────
def test_require_capability_denies_without_cap():
    dep = require_capability("admin")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(dep(principal=Principal(subject="u", caps=frozenset())))
    assert ei.value.status_code == 403


def test_require_capability_allows_with_cap():
    dep = require_capability("admin")
    p = Principal(subject="u", caps=frozenset({"admin"}))
    assert asyncio.run(dep(principal=p)) is p


def test_require_any_capability():
    dep = require_any_capability("a", "b")
    assert asyncio.run(dep(principal=Principal(subject="u", caps=frozenset({"b"})))).subject == "u"
    with pytest.raises(HTTPException):
        asyncio.run(dep(principal=Principal(subject="u", caps=frozenset({"c"}))))
