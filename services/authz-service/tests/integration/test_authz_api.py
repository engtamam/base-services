"""Integration: PAP CRUD + PDP check/effective + scoping."""
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
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
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


def _tok(caps=None, tenant="t1", sub="admin"):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now, "tenant": tenant}
    if caps is not None:
        claims["caps"] = caps
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _plat():
    return {"Authorization": "Bearer " + _tok(["platform.authz"])}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def test_full_rbac_flow(client):
    role = client.post("/api/v1/roles", json={"name": "editor", "capabilities": ["docs.*"], "tenant": "t1"},
                       headers=_plat()).json()
    client.put("/api/v1/subjects/u1/roles", json={"roles": [role["id"]]}, headers=_plat())
    eff = client.get("/internal/authz/effective?subject=u1&tenant=t1", headers=_internal()).json()
    assert eff["capabilities"] == ["docs.*"]
    chk = client.post("/internal/authz/check",
                      json={"subject": "u1", "action": "docs.edit", "resource": "d", "tenant": "t1"},
                      headers=_internal())
    assert chk.json()["allow"] is True


def test_operator_reads_a_policy_and_a_subjects_effective_caps(client):
    role = client.post("/api/v1/roles",
                       json={"name": "viewer", "capabilities": ["docs.read"], "tenant": "t1"},
                       headers=_plat()).json()
    client.put("/api/v1/subjects/u_eff/roles",
               json={"roles": [role["id"]], "capabilities": ["extra.cap"]}, headers=_plat())

    # The operator can now see, on the PANEL, what the subject can actually do (was internal-only).
    eff = client.get("/api/v1/subjects/u_eff/effective", headers=_plat())
    assert eff.status_code == 200
    assert set(eff.json()["capabilities"]) == {"docs.read", "extra.cap"}

    # And view a single policy's detail (was list/delete only).
    pid = client.post("/api/v1/policies",
                      json={"effect": "deny", "action": "x.*", "resource": "*", "tenant": "t1"},
                      headers=_plat()).json()["id"]
    got = client.get(f"/api/v1/policies/{pid}", headers=_plat())
    assert got.status_code == 200 and got.json()["effect"] == "deny"
    assert client.get("/api/v1/policies/nope", headers=_plat()).status_code == 404

    # Both need the capability.
    assert client.get("/api/v1/subjects/u_eff/effective",
                      headers={"Authorization": "Bearer " + _tok(["unrelated"])}).status_code == 403


def test_abac_deny(client):
    role = client.post("/api/v1/roles", json={"name": "w", "capabilities": ["files.*"], "tenant": "t1"},
                       headers=_plat()).json()
    client.put("/api/v1/subjects/u2/roles", json={"roles": [role["id"]]}, headers=_plat())
    client.post("/api/v1/policies",
                json={"effect": "deny", "action": "files.*", "resource": "*",
                      "condition": {"attr": "resource.secret", "op": "eq", "value": True}, "tenant": "t1"},
                headers=_plat())
    d = client.post("/internal/authz/check",
                    json={"subject": "u2", "action": "files.read", "resource": "x", "tenant": "t1",
                          "attributes": {"resource.secret": True}}, headers=_internal())
    assert d.json()["allow"] is False and d.json()["matched_policy"]


def test_pdp_requires_token(client):
    assert client.post("/internal/authz/check", json={"action": "x"}).status_code == 401


def test_crud_needs_capability(client):
    assert client.post("/api/v1/roles", json={"name": "x", "capabilities": []},
                       headers={"Authorization": "Bearer " + _tok([])}).status_code == 403


def test_tenant_admin_scoped(client):
    # authz.manage is forced to the caller's own tenant, even if body says otherwise
    own = {"Authorization": "Bearer " + _tok(["authz.manage"], tenant="tenant-A")}
    r = client.post("/api/v1/roles", json={"name": "r", "capabilities": [], "tenant": "tenant-B"}, headers=own)
    assert r.status_code == 201 and r.json()["tenant"] == "tenant-A"  # body tenant ignored
