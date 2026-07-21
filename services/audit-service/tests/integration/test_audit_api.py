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


# ── scheduled chain verification (internal, the v0.8.1 fix) ────────────────────
def test_internal_verify_requires_the_internal_token(client):
    assert client.post("/internal/audit/verify").status_code == 401
    # a user token, even platform.audit, is not the internal path
    assert client.post("/internal/audit/verify", headers=_platform()).status_code == 401


def test_internal_verify_passes_on_a_clean_chain(client):
    _rec(client, service="s", action="a", actor="x")
    r = client.post("/internal/audit/verify", headers=_internal())
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["partitions_checked"] >= 1


def test_internal_verify_returns_409_on_a_broken_chain():
    # A FRESH app: this test deliberately corrupts the chain, so it must not run against the shared
    # module client — a broken chain there would fail every later test that verifies.
    client = _make_app()
    _rec(client, service="s", action="a", actor="x")
    ds = client.app.state.container.datastore
    import asyncio
    rows = asyncio.run(ds.list("audit_events", 10000))
    target = max(rows, key=lambda r: r.get("seq", 0))
    target["service"] = "tampered"
    asyncio.run(ds.put("audit_events", target["id"], target))

    r = client.post("/internal/audit/verify", headers=_internal())
    # 409 is the whole contract: the cron caller raises on non-2xx, so this fails the Job and
    # alerts. The break detail is logged server-side, not returned (the platform sanitises error
    # bodies), so a broken chain cannot pass for a clean one.
    assert r.status_code == 409, "a broken chain must fail loudly, not 200"


# ── erase accepts either the raw subject or the pseudonym (the v0.8.1 enhancement) ─
def test_erase_with_the_standard_subject_body_acks(client):
    # Exactly what the compliance dispatcher sends: tenant+subject (+ ignored extras).
    r = client.post("/internal/privacy/erase",
                    json={"tenant": "acme", "subject": "u@example.com",
                          "action": "erase", "request_id": "r1"}, headers=_internal())
    assert r.status_code == 200
    body = r.json()
    assert body["erased"] == [] and body["chain_verifies"] is True
    # The ack carries the PSEUDONYM, never the raw email that was sent.
    assert body["subject_ref"].startswith("sub_") and "u@example.com" not in body["subject_ref"]


def test_erase_accepts_a_subject_ref_directly(client):
    # A caller that holds only the pseudonym (identity already erased the linkage) can still erase.
    r = client.post("/internal/privacy/erase",
                    json={"tenant": "acme", "subject_ref": "sub_deadbeef"}, headers=_internal())
    assert r.status_code == 200
    assert r.json()["subject_ref"] == "sub_deadbeef"


def test_erase_requires_exactly_one_of_subject_or_subject_ref(client):
    # neither → 422
    assert client.post("/internal/privacy/erase", json={"tenant": "acme"},
                       headers=_internal()).status_code == 422
    # both → 422 (a mismatched pair must not ack against the wrong person)
    assert client.post("/internal/privacy/erase",
                       json={"tenant": "acme", "subject": "u@x.com", "subject_ref": "sub_x"},
                       headers=_internal()).status_code == 422
