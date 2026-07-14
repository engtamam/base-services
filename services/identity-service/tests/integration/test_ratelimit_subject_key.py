"""The limiter keys on the SUBJECT even when it is the FIRST dependency to run.

The bug this guards: reading request.state.principal only works if the auth dependency ran first.
FastAPI runs decorator dependencies (where rate_limit lives) BEFORE parameter dependencies (where
auth lives), so the naive version saw None and everyone behind one NAT shared an IP bucket. The
limiter now resolves the subject itself, so ordering cannot break it.
"""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

SECRET = "s" * 48


@pytest.fixture
def client(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
                 "RATELIMIT_MAX": "2", "RATELIMIT_WINDOW_SECONDS": "60",
                 "RATELIMITER_DRIVER": "memory",
                 "RATELIMIT_KEY_PARTS": "subject,ip",
                 "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api.ratelimit import rate_limit
    from app.container import build_container

    app = FastAPI()
    app.state.container = build_container(get_settings())

    # rate_limit is the ONLY dependency — no auth runs before it. If the limiter relied on
    # request.state.principal, the subject would be None here. It must not.
    @app.get("/thing", dependencies=[Depends(rate_limit)])
    async def thing():
        return {"ok": True}

    yield TestClient(app, raise_server_exceptions=False)
    get_settings.cache_clear()


def _tok(sub, tenant="acme"):
    from app.config import get_settings
    s = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "tenant": tenant, "type": "access",
              "iat": now, "exp": now + dt.timedelta(minutes=5)}
    if s.TOKEN_ISSUER:
        claims["iss"] = s.TOKEN_ISSUER
    if s.TOKEN_AUDIENCE:
        claims["aud"] = s.TOKEN_AUDIENCE
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_two_subjects_behind_one_ip_get_separate_budgets(client):
    """The office-NAT case. Both callers share a source IP; keyed on subject they must not share a
    bucket — which only holds if the limiter resolved the subject WITHOUT an auth dependency
    running first (there is none on /thing)."""
    a = {"Authorization": f"Bearer {_tok('alice')}"}
    b = {"Authorization": f"Bearer {_tok('bob')}"}

    # alice spends her whole budget (limit 2)
    assert client.get("/thing", headers=a).status_code == 200
    assert client.get("/thing", headers=a).status_code == 200
    assert client.get("/thing", headers=a).status_code == 429     # alice throttled

    # bob, same IP, still has his full budget — the subject was keyed, not the shared IP
    assert client.get("/thing", headers=b).status_code == 200
    assert client.get("/thing", headers=b).status_code == 200


def test_anonymous_falls_back_to_ip(client):
    """No token → None subject → IP-keyed. A deliberate fallback, so /login is still protected."""
    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 429               # same IP, one anon bucket


def test_an_invalid_token_is_treated_as_anonymous_not_a_500(client):
    """A garbage bearer must not crash the limiter — it resolves to None and IP-keys."""
    r = client.get("/thing", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 200
