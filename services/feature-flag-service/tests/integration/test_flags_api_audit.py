"""API regressions for feature-flag audit findings #02 (user-scope override) and #05 (evaluate scope)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _adm(caps=("platform.flags",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "admin", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


# ── 02: a non-platform admin cannot set a (cross-tenant) user override ─────────
def test_user_override_requires_platform(client):
    client.post("/api/v1/flags", json={"key": "uf", "enabled": True}, headers=_adm())
    # a tenant admin (flags.manage, not platform) is refused a user-scope override
    r = client.post("/api/v1/flags/uf/overrides",
                    json={"scope": "user", "subject": "anyuser", "value": True},
                    headers=_adm(caps=("flags.manage",), tenant="tenant-A"))
    assert r.status_code == 403
    # a platform operator may
    r2 = client.post("/api/v1/flags/uf/overrides",
                     json={"scope": "user", "subject": "anyuser", "value": True}, headers=_adm())
    assert r2.status_code == 200


# ── 05: evaluate is scoped to the caller's tenant for a non-platform reader ────
def test_evaluate_is_scoped_to_caller_tenant(client):
    # a flag (master ON) with a tenant override for 'victim'
    client.post("/api/v1/flags", json={"key": "ef", "enabled": True, "default": False,
                                       "rollout_pct": 0}, headers=_adm())
    client.post("/api/v1/flags/ef/overrides",
                json={"scope": "tenant", "subject": "victim", "value": True}, headers=_adm())
    # an attacker (flags.read) in another tenant asks to evaluate AS 'victim' — must be scoped to self,
    # so it must NOT observe victim's tenant_override (the reason field is the leak vector)
    reader = _adm(caps=("flags.read",), tenant="attacker")
    body = client.post("/api/v1/flags/evaluate",
                       json={"keys": ["ef"], "tenant": "victim", "user": "u"}, headers=reader).json()
    assert body["flags"]["ef"]["reason"] != "tenant_override"
    # a platform reader may target victim and DOES observe the override
    plat = _adm(caps=("platform.flags", "flags.read"))
    pbody = client.post("/api/v1/flags/evaluate",
                        json={"keys": ["ef"], "tenant": "victim", "user": "u"}, headers=plat).json()
    assert pbody["flags"]["ef"]["reason"] == "tenant_override"
