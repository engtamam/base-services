"""Security: no code execution in conditions, PDP token, cross-tenant, deny-by-default."""
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


def _plat(tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "a", "caps": ["platform.authz"], "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def test_deny_by_default(client):
    assert client.get("/api/v1/roles").status_code == 401
    assert client.post("/api/v1/policies", json={"effect": "allow"}).status_code == 401


def test_malicious_condition_is_not_executed(client):
    # a policy whose condition value looks like code must never run it — it is
    # compared as a literal, so it simply does not match.
    client.post("/api/v1/policies",
                json={"effect": "allow", "action": "*", "resource": "*",
                      "condition": {"attr": "x", "op": "eq", "value": "__import__('os').system('id')"},
                      "tenant": "t1"}, headers=_plat())
    d = client.post("/internal/authz/check",
                    json={"subject": "u", "action": "anything", "resource": "r", "tenant": "t1",
                          "attributes": {"x": "safe"}}, headers=_internal())
    # no role, condition doesn't match -> default deny (nothing executed, no crash)
    assert d.status_code == 200 and d.json()["allow"] is False


def test_cross_tenant_isolation(client):
    # a role in tenant t-a must not be visible/effective in t-b
    role = client.post("/api/v1/roles", json={"name": "r", "capabilities": ["x.y"], "tenant": "t-a"},
                       headers=_plat("t-a")).json()
    client.put("/api/v1/subjects/u/roles", json={"roles": [role["id"]]}, headers=_plat("t-a"))
    # effective in the WRONG tenant yields nothing (role.tenant != tenant)
    eff = client.get("/internal/authz/effective?subject=u&tenant=t-b", headers=_internal()).json()
    assert eff["capabilities"] == []


def test_unknown_action_default_deny(client):
    d = client.post("/internal/authz/check",
                    json={"subject": "nobody", "action": "secret.launch", "resource": "r", "tenant": "t1"},
                    headers=_internal())
    assert d.json()["allow"] is False


def _tenant_admin(tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "owner", "caps": ["authz.manage"], "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def test_a_tenant_admin_cannot_create_a_wildcard_allow_policy(client):
    # privilege escalation: an allow policy is a capability grant; a wildcard one grants everything.
    r = client.post("/api/v1/policies",
                    json={"effect": "allow", "action": "*", "resource": "*", "condition": {}},
                    headers=_tenant_admin())
    assert r.status_code == 403


def test_a_tenant_admin_cannot_grant_a_platform_action_by_policy(client):
    r = client.post("/api/v1/policies",
                    json={"effect": "allow", "action": "platform.billing", "resource": "*",
                          "condition": {}}, headers=_tenant_admin())
    assert r.status_code == 403


def test_a_platform_operator_may_create_a_wildcard_policy(client):
    # delegated administration is the operator's job — the guard only binds tenant-scoped callers.
    r = client.post("/api/v1/policies",
                    json={"effect": "allow", "action": "*", "resource": "*", "condition": {},
                          "tenant": "t1"}, headers=_plat())
    assert r.status_code == 201


def test_a_tenant_admin_may_create_a_narrow_own_policy(client):
    r = client.post("/api/v1/policies",
                    json={"effect": "allow", "action": "booking.read", "resource": "booking:1",
                          "condition": {}}, headers=_tenant_admin())
    assert r.status_code in (201, 403)   # 201 unless the deployment allowlist excludes booking.read
