"""GET /api/v1/admin/catalog over HTTP: cap-gated, browser uses a user JWT, results filtered."""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 48


@pytest.fixture
def client(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
                 "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
                 "ADMIN_SERVICES": "tenant-service=http://tenant-service:8080",
                 "ADMIN_CATALOG_TTL": "0"}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()

    # Stub the network: pretend tenant-service returned its manifest with one admin action.
    import app.api.v1.admin as adm

    async def fake_fetch_via_build(targets, caps, fetch, **kw):
        from app.core.admin_catalog import build_catalog

        async def stub(_t):
            return {"admin_actions": [
                {"id": "tenant.suspend", "label": "Suspend", "method": "POST",
                 "path": "/api/v1/tenants/{id}/suspend", "capability": "platform.tenants",
                 "danger": True},
                {"id": "billing.refund", "label": "Refund", "method": "POST",
                 "path": "/x", "capability": "platform.billing"},
            ]}
        return await build_catalog(targets, caps, stub, **kw)

    monkeypatch.setattr(adm, "build_catalog", fake_fetch_via_build)

    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    get_settings.cache_clear()


def _tok(caps):
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode({"sub": "op", "caps": caps, "type": "access", "iat": now,
                       "exp": now + dt.timedelta(minutes=5),
                       "iss": "api-gateway", "aud": "base-services"}, SECRET, algorithm="HS256")


def _h(caps):
    return {"Authorization": f"Bearer {_tok(caps)}"}


def test_opening_the_panel_requires_the_admin_cap(client):
    # a user with no platform.admin cap cannot open the catalog at all (T5)
    r = client.get("/api/v1/admin/catalog", headers=_h(["billing.self"]))
    assert r.status_code == 403


def test_no_token_is_401(client):
    assert client.get("/api/v1/admin/catalog").status_code == 401


def test_catalog_returns_only_actions_the_operator_can_use(client):
    # holds platform.admin (to open) + platform.tenants (to see tenant actions), NOT platform.billing
    r = client.get("/api/v1/admin/catalog", headers=_h(["platform.admin", "platform.tenants"]))
    assert r.status_code == 200
    ids = [a["id"] for s in r.json()["services"] for a in s["actions"]]
    assert "tenant.suspend" in ids
    assert "billing.refund" not in ids, "an action whose cap the operator lacks must not appear"
