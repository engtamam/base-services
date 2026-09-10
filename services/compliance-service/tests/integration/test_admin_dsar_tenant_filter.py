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
    # Without the param the operator sees only their own (platform) tenant — the reported bug: empty.
    own = app.get("/api/v1/compliance/dsar", headers=plat).json()["requests"]
    assert all(r.get("tenant") != "shopx" for r in own)
    # With ?tenant=shopx the operator now sees the shop's DSAR.
    got = app.get("/api/v1/compliance/dsar?tenant=shopx", headers=plat).json()["requests"]
    assert any(r.get("subject") == "cust@x.com" or r.get("tenant") == "shopx" for r in got)
    assert len(got) >= 1


def test_tenant_admins_tenant_param_is_ignored_no_cross_tenant_read(app):
    # A DSAR under 'shopy'.
    app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
             headers=_auth("c@y.com", "shopy", "compliance.self"))
    # A tenant admin of 'other' tries ?tenant=shopy — the param is ignored, they stay scoped to 'other'.
    other = _auth("admin@other", "other", "compliance.read")
    got = app.get("/api/v1/compliance/dsar?tenant=shopy", headers=other).json()["requests"]
    assert all(r.get("tenant") != "shopy" for r in got)   # no cross-tenant leak
