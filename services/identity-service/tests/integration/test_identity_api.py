"""Integration: the /api/v1/auth flows end-to-end (in-process)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

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
def env_guard():
    from app.config import get_settings

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(env_guard) -> TestClient:
    return _make_app()


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_register_login_me(client):
    r = client.post("/api/v1/auth/register", json={"username": "alice", "password": "password1"})
    assert r.status_code == 201
    assert "password_hash" not in r.json()

    r = client.post("/api/v1/auth/login", json={"username": "alice", "password": "password1"})
    assert r.status_code == 200
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    me = client.get("/api/v1/auth/me", headers=_auth(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert "password_hash" not in me.json()


def _login(client, username="pref", password="password1"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    return client.post("/api/v1/auth/login",
                       json={"username": username, "password": password}).json()["access_token"]


def test_update_and_read_my_preferences(client):
    tok = _login(client, "prefuser")
    r = client.patch("/api/v1/auth/me/preferences",
                     json={"preferences": {"locale": "ar", "theme": "dark"}}, headers=_auth(tok))
    assert r.status_code == 200
    assert r.json()["preferences"] == {"locale": "ar", "theme": "dark"}
    # persisted + a MERGE: a second patch updates one key, leaves the other, null clears a key
    r = client.patch("/api/v1/auth/me/preferences",
                     json={"preferences": {"locale": "en", "theme": None}}, headers=_auth(tok))
    assert r.json()["preferences"] == {"locale": "en"}
    assert client.get("/api/v1/auth/me", headers=_auth(tok)).json()["preferences"] == {"locale": "en"}


def test_preferences_requires_a_token(client):
    assert client.patch("/api/v1/auth/me/preferences",
                        json={"preferences": {"locale": "ar"}}).status_code == 401


def test_preferences_reject_non_scalar_and_oversize(client):
    tok = _login(client, "prefbad")
    assert client.patch("/api/v1/auth/me/preferences",
                        json={"preferences": {"x": {"nested": 1}}}, headers=_auth(tok)).status_code == 400
    assert client.patch("/api/v1/auth/me/preferences",
                        json={"preferences": {"x": "y" * 300}}, headers=_auth(tok)).status_code == 400


def test_duplicate_register_409(client):
    client.post("/api/v1/auth/register", json={"username": "bob", "password": "password1"})
    r = client.post("/api/v1/auth/register", json={"username": "bob", "password": "password1"})
    assert r.status_code == 409


def test_bad_login_401(client):
    client.post("/api/v1/auth/register", json={"username": "carol", "password": "password1"})
    r = client.post("/api/v1/auth/login", json={"username": "carol", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_refresh_rotation(client):
    client.post("/api/v1/auth/register", json={"username": "dave", "password": "password1"})
    tokens = client.post("/api/v1/auth/login", json={"username": "dave", "password": "password1"}).json()
    rt = tokens["refresh_token"]
    new = client.post("/api/v1/auth/refresh", json={"refresh_token": rt})
    assert new.status_code == 200
    # old refresh token is now invalid (rotated)
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt}).status_code == 401


def test_logout_revokes(client):
    client.post("/api/v1/auth/register", json={"username": "erin", "password": "password1"})
    tokens = client.post("/api/v1/auth/login", json={"username": "erin", "password": "password1"}).json()
    rt = tokens["refresh_token"]
    assert client.post("/api/v1/auth/logout", json={"refresh_token": rt}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt}).status_code == 401


def test_rate_limit(env_guard):
    rl = _make_app(RATELIMIT_MAX="2")
    rl.post("/api/v1/auth/register", json={"username": "rluser", "password": "password1"})  # call 1
    first = rl.post("/api/v1/auth/login", json={"username": "rluser", "password": "password1"})   # call 2
    second = rl.post("/api/v1/auth/login", json={"username": "rluser", "password": "password1"})  # call 3
    assert first.status_code == 200
    assert second.status_code == 429
