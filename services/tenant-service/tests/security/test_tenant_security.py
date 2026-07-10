"""Security: validation, cross-tenant isolation, deny-by-default, internal token."""
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

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET,
                      RATELIMIT_MAX="1000", BASE_DOMAIN="apps.example.com")
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


def _tok(caps=None, tenant=None, sub="u"):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    if tenant:
        claims["tenant"] = tenant
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _platform():
    return {"Authorization": "Bearer " + _tok(["platform.tenants"])}


def test_deny_by_default(client):
    assert client.get("/api/v1/tenants").status_code == 401
    assert client.get("/api/v1/tenant").status_code == 401


def test_internal_resolve_requires_token(client):
    assert client.get("/internal/tenants/resolve?host=x.apps.example.com").status_code == 401


def test_invalid_slug_400(client):
    r = client.post("/api/v1/tenants", json={"slug": "Bad_Slug", "name": "x"}, headers=_platform())
    assert r.status_code == 400


def test_cross_tenant_isolation(client):
    a = client.post("/api/v1/tenants", json={"slug": "aaa", "name": "A"}, headers=_platform()).json()["id"]
    b = client.post("/api/v1/tenants", json={"slug": "bbb", "name": "B"}, headers=_platform()).json()["id"]
    # a tenant-admin scoped to A only ever sees A via /api/v1/tenant
    own_a = {"Authorization": "Bearer " + _tok(["tenant.manage"], tenant=a)}
    me = client.get("/api/v1/tenant", headers=own_a)
    assert me.json()["id"] == a and me.json()["id"] != b
    # and cannot reach B's admin route (no platform cap)
    assert client.get(f"/api/v1/tenants/{b}", headers=own_a).status_code == 403


def test_tenant_admin_without_tenant_claim_forbidden(client):
    # tenant.manage cap but no tenant claim -> 403 (can't resolve "own")
    tok = {"Authorization": "Bearer " + _tok(["tenant.manage"])}
    assert client.get("/api/v1/tenant", headers=tok).status_code == 403
