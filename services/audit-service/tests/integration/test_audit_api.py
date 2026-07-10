"""Integration: ingest + query + verify + export + tenant scoping."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
           "RATELIMIT_MAX": "1000", "AUDIT_PARTITION": "global"}
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


def _tok(caps=None, tenant=None, sub="u"):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    if tenant:
        claims["tenant"] = tenant
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _platform():
    return {"Authorization": "Bearer " + _tok(["platform.audit"])}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def _rec(client, **kw):
    return client.post("/internal/audit", json=kw, headers=_internal())


def test_ingest_requires_token(client):
    assert client.post("/internal/audit", json={"service": "s", "action": "a"}).status_code == 401


def test_record_query_verify(client):
    _rec(client, service="identity", action="login", actor="alice")
    _rec(client, service="identity", action="logout", actor="alice")
    q = client.get("/api/v1/audit?actor=alice", headers=_platform())
    assert q.status_code == 200 and q.json()["count"] >= 2
    v = client.get("/api/v1/audit/verify", headers=_platform())
    assert v.json()["ok"] is True


def test_export_csv(client):
    _rec(client, service="s", action="x")
    r = client.get("/api/v1/audit/export?format=csv", headers=_platform())
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]


def test_query_needs_a_capability(client):
    # a token with neither platform.audit nor audit.view -> 403
    assert client.get("/api/v1/audit", headers={"Authorization": "Bearer " + _tok([])}).status_code == 403


def test_verify_needs_platform_cap(client):
    assert client.get("/api/v1/audit/verify",
                      headers={"Authorization": "Bearer " + _tok(["audit.view"], tenant="t1")}).status_code == 403


def test_tenant_admin_scoped(client):
    tenant_admin = _make_app(AUDIT_PARTITION="tenant")
    # two tenants' events
    tenant_admin.post("/internal/audit", json={"service": "s", "action": "a", "tenant": "t1"}, headers=_internal())
    tenant_admin.post("/internal/audit", json={"service": "s", "action": "b", "tenant": "t2"}, headers=_internal())
    own = {"Authorization": "Bearer " + _tok(["audit.view"], tenant="t1")}
    r = tenant_admin.get("/api/v1/audit", headers=own)
    tenants = {e["tenant"] for e in r.json()["events"]}
    assert tenants == {"t1"}  # never sees t2
