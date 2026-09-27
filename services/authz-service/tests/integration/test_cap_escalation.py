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


def _tenant_admin(extra_caps=()):
    # A tenant admin can only hand out capabilities it itself holds (caps<=granter), so tests that
    # assign benign caps must give the admin those caps too.
    return {"Authorization": "Bearer " + _tok(["authz.manage", *extra_caps])}


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
    r = app.post("/api/v1/roles", headers=_tenant_admin(["billing.self", "tickets.write"]),
                 json={"name": "support", "capabilities": ["billing.self", "tickets.write"]})
    assert r.status_code == 201


def test_operator_is_unrestricted(env_guard):
    app = _make_app()
    r = app.post("/api/v1/roles", headers=_operator(),
                 json={"name": "admins", "capabilities": ["platform.authz", "authz.manage"],
                       "tenant": "tenant-A"})
    assert r.status_code == 201


# ── caps<=granter: the subset rule (authz-r2-01) ──────────────────────────────
def test_tenant_admin_cannot_grant_a_cap_it_does_not_hold(env_guard):
    # Caller holds {a,b}; granting {a,c} must be refused for c (a caller can never grant above itself).
    app = _make_app()
    hdr = _tenant_admin(["wallet.read", "billing.self"])  # holds a=wallet.read, b=billing.self
    r = app.post("/api/v1/roles", headers=hdr,
                 json={"name": "escalate", "capabilities": ["wallet.read", "wallet.payout"]})
    # Refused because wallet.payout is not held; wallet.read (held) alone would have been fine.
    # (The 403 body is scrubbed to a generic FORBIDDEN by middleware, so we assert on status only.)
    assert r.status_code == 403


def test_tenant_admin_can_grant_exactly_what_it_holds(env_guard):
    app = _make_app()
    hdr = _tenant_admin(["wallet.read", "billing.self"])
    r = app.post("/api/v1/roles", headers=hdr,
                 json={"name": "ok", "capabilities": ["wallet.read", "billing.self"]})
    assert r.status_code == 201


def test_tenant_admin_wildcard_holding_grants_within_it(env_guard):
    # Holding files.* lets the caller grant files.read, but not wallet.payout.
    app = _make_app()
    hdr = _tenant_admin(["files.*"])
    ok = app.post("/api/v1/roles", headers=hdr,
                  json={"name": "f", "capabilities": ["files.read", "files.write"]})
    assert ok.status_code == 201
    bad = app.post("/api/v1/roles", headers=hdr,
                   json={"name": "b", "capabilities": ["wallet.payout"]})
    assert bad.status_code == 403


def test_direct_subject_grant_is_subset_gated(env_guard):
    app = _make_app()
    hdr = _tenant_admin(["billing.self"])
    r = app.put("/api/v1/subjects/x/roles", headers=hdr,
                json={"roles": [], "capabilities": ["billing.refund"]})
    assert r.status_code == 403


# ── FIN authz-002: assigning a ROLE whose caps exceed the granter is refused ───────────────────────
def test_tenant_admin_cannot_assign_a_role_whose_caps_exceed_its_own(env_guard):
    app = _make_app()
    # operator creates a privileged role in tenant-A
    priv = app.post("/api/v1/roles", headers=_operator(),
                    json={"name": "payouts", "capabilities": ["wallet.payout"], "tenant": "tenant-A"})
    assert priv.status_code == 201
    rid = priv.json()["id"]
    # a tenant admin holding only billing.self tries to ASSIGN that role (not direct caps) → refused,
    # because the role carries wallet.payout which the admin does not hold (the caps<=granter gate now
    # covers body.roles, not just body.capabilities).
    hdr = _tenant_admin(["billing.self"])
    r = app.put("/api/v1/subjects/victim/roles", headers=hdr, json={"roles": [rid], "capabilities": []})
    assert r.status_code == 403


def test_tenant_admin_may_assign_a_role_within_its_caps(env_guard):
    app = _make_app()
    ok_role = app.post("/api/v1/roles", headers=_operator(),
                       json={"name": "support", "capabilities": ["billing.self"], "tenant": "tenant-A"})
    rid = ok_role.json()["id"]
    hdr = _tenant_admin(["billing.self"])
    r = app.put("/api/v1/subjects/agent/roles", headers=hdr, json={"roles": [rid], "capabilities": []})
    assert r.status_code == 200


def test_bootstrap_route_rejects_the_star_wildcard(env_guard):
    # FIN authz-001 at the route: the internal bootstrap must refuse a role carrying "*".
    app = _make_app()
    r = app.post("/internal/authz/tenants/tenant-Z/bootstrap",
                 headers={"X-Internal-Token": ITOKEN},
                 json={"subject": "owner-1", "assign": ["owner"],
                       "roles": [{"name": "owner", "capabilities": ["*"]}]})
    assert r.status_code == 400


def test_denylist_beats_subset_even_if_caller_holds_the_platform_cap(env_guard):
    # A tenant caller that somehow holds platform.foo (but not platform.authz, so not an operator)
    # still cannot hand it out: the platform.* wall refuses it independent of the subset rule.
    app = _make_app()
    hdr = {"Authorization": "Bearer " + _tok(["authz.manage", "platform.foo"])}
    r = app.post("/api/v1/roles", headers=hdr,
                 json={"name": "sneaky", "capabilities": ["platform.foo"]})
    assert r.status_code == 403


# ── ALLOW-policy vector inherits the subset gate (authz-r2-02) ─────────────────
def test_tenant_admin_cannot_allow_an_action_it_does_not_hold(env_guard):
    app = _make_app()
    hdr = _tenant_admin(["billing.self"])
    r = app.post("/api/v1/policies", headers=hdr,
                 json={"effect": "allow", "action": "compliance.erase", "resource": "subject:x"})
    assert r.status_code == 403


def test_tenant_admin_may_allow_an_action_it_holds(env_guard):
    app = _make_app()
    hdr = _tenant_admin(["reports.export"])
    r = app.post("/api/v1/policies", headers=hdr,
                 json={"effect": "allow", "action": "reports.export", "resource": "report:1"})
    assert r.status_code == 201


# ── the optional positive allowlist ───────────────────────────────────────────
def test_a_configured_allowlist_rejects_anything_outside_it(env_guard):
    app = _make_app(AUTHZ_TENANT_ASSIGNABLE_CAPS="billing.self,tickets.write")
    # Caller holds both caps, so the subset rule passes and the ALLOWLIST is what decides.
    hdr = _tenant_admin(["billing.self", "reports.export"])
    ok = app.post("/api/v1/roles", headers=hdr,
                  json={"name": "ok", "capabilities": ["billing.self"]})
    assert ok.status_code == 201
    bad = app.post("/api/v1/roles", headers=hdr,
                   json={"name": "bad", "capabilities": ["reports.export"]})
    assert bad.status_code == 403
