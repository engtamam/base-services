"""A tenant admin cannot assign capabilities that escalate beyond their tenant.

The gap this pins: every route that accepts `capabilities` (create_role, update_role, and the DIRECT
subject grant) stored them verbatim. An `authz.manage` holder — forced to their own tenant on the
ROW — could still put `platform.*` / `authz.manage` in a role and assign it to themselves, escalating
out of the tenant. These tests prove the server refuses it, and that a platform operator stays free.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**env) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000",
                      **env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _tok(caps, tenant="tenant-A", sub="owner"):
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode({"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now,
                       "tenant": tenant, "caps": caps}, SECRET, algorithm="HS256")


def _tenant_admin():
    return {"Authorization": "Bearer " + _tok(["authz.manage"])}


def _operator():
    return {"Authorization": "Bearer " + _tok(["platform.authz"])}


# ── the escalation is refused ─────────────────────────────────────────────────
@pytest.mark.parametrize("cap", ["platform.authz", "platform.operators", "authz.manage"])
def test_tenant_admin_cannot_create_a_role_with_a_platform_cap(env_guard, cap):
    app = _make_app()
    r = app.post("/api/v1/roles", headers=_tenant_admin(),
                 json={"name": "sneaky", "capabilities": ["billing.self", cap]})
    assert r.status_code == 403


def test_tenant_admin_cannot_grant_a_platform_cap_directly_to_a_subject(env_guard):
    app = _make_app()
    r = app.put("/api/v1/subjects/evil/roles", headers=_tenant_admin(),
                json={"roles": [], "capabilities": ["platform.authz"]})
    assert r.status_code == 403


def test_tenant_admin_cannot_update_a_role_to_add_a_platform_cap(env_guard):
    app = _make_app()
    # first create a legit role as the operator
    created = app.post("/api/v1/roles", headers=_operator(),
                       json={"name": "support", "capabilities": ["billing.self"],
                             "tenant": "tenant-A"})
    rid = created.json()["id"]
    r = app.put(f"/api/v1/roles/{rid}", headers=_tenant_admin(),
                json={"capabilities": ["billing.self", "platform.authz"]})
    assert r.status_code == 403


# ── legitimate cases still work ───────────────────────────────────────────────
def test_tenant_admin_may_assign_ordinary_tenant_caps(env_guard):
    app = _make_app()
    r = app.post("/api/v1/roles", headers=_tenant_admin(),
                 json={"name": "support", "capabilities": ["billing.self", "tickets.write"]})
    assert r.status_code == 201


def test_operator_is_unrestricted(env_guard):
    app = _make_app()
    r = app.post("/api/v1/roles", headers=_operator(),
                 json={"name": "admins", "capabilities": ["platform.authz", "authz.manage"],
                       "tenant": "tenant-A"})
    assert r.status_code == 201


# ── the optional positive allowlist ───────────────────────────────────────────
def test_a_configured_allowlist_rejects_anything_outside_it(env_guard):
    app = _make_app(AUTHZ_TENANT_ASSIGNABLE_CAPS="billing.self,tickets.write")
    ok = app.post("/api/v1/roles", headers=_tenant_admin(),
                  json={"name": "ok", "capabilities": ["billing.self"]})
    assert ok.status_code == 201
    bad = app.post("/api/v1/roles", headers=_tenant_admin(),
                   json={"name": "bad", "capabilities": ["reports.export"]})
    assert bad.status_code == 403
