"""A platform operator can list a specific shop's DSARs via ?tenant=; a tenant admin cannot.

The reported bug: GET /api/v1/compliance/dsar is a GET with no body, so _tenant() gave a platform
operator only their own JWT tenant's DSARs — a customer's self-service DSAR (filed under the shop's
real tenant) never showed in the admin panel. The fix adds an optional `tenant` query param, honored
exactly like the POST routes: platform-only, a tenant admin stays scoped to their own tenant.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": "unit-test-kek", "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(sub: str, tenant: str, *caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": sub, "iss": ISS, "aud": AUD, "caps": list(caps),
                      "tenant": tenant, "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def app():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def test_platform_operator_lists_a_specific_shops_dsars(app):
    # A customer of shop 'shopx' files a DSAR against themselves (tenant = their shop).
    assert app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
                    headers=_auth("cust@x.com", "shopx", "compliance.self")).status_code == 201

    plat = _auth("op@platform", "platform", "platform.compliance")
    # With ?tenant=shopx the operator sees that shop's DSAR (scoped).
    got = app.get("/api/v1/compliance/dsar?tenant=shopx", headers=plat).json()["requests"]
    assert any(r.get("subject") == "cust@x.com" or r.get("tenant") == "shopx" for r in got)
    assert len(got) >= 1


def test_platform_operator_no_tenant_gets_cross_tenant_open_dsars(app):
    # The reported bug's real fix: a customer's DSAR must be DISCOVERABLE without knowing the shop.
    app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
             headers=_auth("disco@z.com", "shopz", "compliance.self"))
    plat = _auth("op@platform", "platform", "platform.compliance")
    body = app.get("/api/v1/compliance/dsar", headers=plat).json()   # NO ?tenant
    assert body["scope"] == "all_open"
    assert any(r.get("tenant") == "shopz" for r in body["requests"])   # every shop's open rows


def test_tenant_admins_tenant_param_is_ignored_no_cross_tenant_read(app):
    # A DSAR under 'shopy'.
    app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
             headers=_auth("c@y.com", "shopy", "compliance.self"))
    # A tenant admin of 'other' tries ?tenant=shopy — the param is ignored, they stay scoped to 'other'.
    other = _auth("admin@other", "other", "compliance.read")
    got = app.get("/api/v1/compliance/dsar?tenant=shopy", headers=other).json()["requests"]
    assert all(r.get("tenant") != "shopy" for r in got)   # no cross-tenant leak


def test_tenant_admin_no_tenant_gets_no_cross_tenant_view(app):
    # A non-platform caller with NO tenant param must NOT get the cross-tenant all-open view.
    app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
             headers=_auth("c@w.com", "shopw", "compliance.self"))
    other = _auth("admin@other2", "other2", "compliance.read")
    body = app.get("/api/v1/compliance/dsar", headers=other).json()
    assert body.get("scope") != "all_open"                # scoped to own tenant, not cross-tenant
    assert all(r.get("tenant") != "shopw" for r in body["requests"])


def test_platform_operator_can_verify_and_process_a_discovered_dsar(app):
    # audit #04: an officer who DISCOVERS another shop's DSAR must be able to advance it past its PDPL
    # clock. Before the fix verify/process took no tenant and scoped to the operator's own reserved
    # tenant, so a discovered request could never be verified or processed through the platform surface.
    created = app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
                       headers=_auth("act@shopq", "shopq", "compliance.self")).json()
    dsar_id, token = created["id"], created["verification_token"]
    plat = _auth("op@platform", "platform", "platform.compliance")

    # Without ?tenant the operator is scoped to their own reserved tenant -> cannot reach shopq's DSAR.
    miss = app.post(f"/api/v1/compliance/dsar/{dsar_id}/verify?token={token}", headers=plat)
    assert miss.status_code == 400

    # With ?tenant=shopq the operator verifies and then processes the discovered request.
    ok = app.post(f"/api/v1/compliance/dsar/{dsar_id}/verify?token={token}&tenant=shopq", headers=plat)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    proc = app.post(f"/api/v1/compliance/dsar/{dsar_id}/process?tenant=shopq", headers=plat)
    assert proc.status_code == 200


def test_platform_operator_lists_cross_tenant_active_holds(app):
    # Two shops each get a legal hold; the platform officer sees both with no ?tenant.
    plat = _auth("op@platform", "platform", "platform.compliance")
    for shop, subj in (("holdshop1", "a@1"), ("holdshop2", "b@2")):
        app.post("/api/v1/compliance/legal-holds",
                 json={"tenant": shop, "subject": subj, "reason": "litigation"}, headers=plat)
    body = app.get("/api/v1/compliance/legal-holds", headers=plat).json()
    assert body["scope"] == "all_active"
    tenants = {h.get("tenant") for h in body["holds"]}
    assert {"holdshop1", "holdshop2"} <= tenants
