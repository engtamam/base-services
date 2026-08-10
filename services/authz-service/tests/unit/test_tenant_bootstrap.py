"""Internal tenant bootstrap — breaks the closed cycle where only a holder of authz.manage can create
the role that grants authz.manage, so a self-service signup could mint a token with caps:[]."""
from __future__ import annotations

from starlette.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def _client():
    return TestClient(create_app())


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def _eff(c, subject, tenant):
    return sorted(c.get("/internal/authz/effective",
                        params={"subject": subject, "tenant": tenant, "roles": ""},
                        headers=_tok()).json()["capabilities"])


def test_bootstrap_makes_a_new_owner_an_admin():
    c = _client(); T = "acme-new"; S = "owner-1"
    assert _eff(c, S, T) == []                                   # the bug: brand-new tenant, no caps
    r = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json={
        "roles": [{"name": "owner", "capabilities": ["authz.manage", "billing.read"]}],
        "subject": S, "assign": ["owner"]})
    assert r.status_code == 200
    # the owner's authz assignment now carries the admin caps → the next token identity mints has them
    assert _eff(c, S, T) == ["authz.manage", "billing.read"]


def test_it_is_idempotent():
    c = _client(); T = "acme-idem"; S = "owner-2"
    body = {"roles": [{"name": "owner", "capabilities": ["authz.manage"]}], "subject": S, "assign": ["owner"]}
    r1 = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json=body)
    r2 = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json=body)
    assert r1.status_code == r2.status_code == 200
    # same role id across runs (updated in place, not duplicated), same caps
    assert r1.json()["roles"][0]["id"] == r2.json()["roles"][0]["id"]
    assert _eff(c, S, T) == ["authz.manage"]


def test_platform_capabilities_are_refused_but_authz_manage_is_allowed():
    c = _client(); T = "acme-guard"
    ok = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json={
        "roles": [{"name": "owner", "capabilities": ["authz.manage"]}], "subject": "o", "assign": ["owner"]})
    assert ok.status_code == 200                                 # tenant admin is fine at the internal tier
    bad = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json={
        "roles": [{"name": "god", "capabilities": ["platform.authz"]}], "subject": "o", "assign": []})
    assert bad.status_code == 400                               # platform.* stays forbidden (escalation guard)


def test_assign_must_reference_a_defined_role():
    c = _client(); T = "acme-ref"
    r = c.post(f"/internal/authz/tenants/{T}/bootstrap", headers=_tok(), json={
        "roles": [{"name": "owner", "capabilities": []}], "subject": "o", "assign": ["missing"]})
    assert r.status_code == 400


def test_requires_the_internal_token():
    c = _client()
    r = c.post("/internal/authz/tenants/t/bootstrap", json={"roles": [], "subject": "o"})
    assert r.status_code == 401
