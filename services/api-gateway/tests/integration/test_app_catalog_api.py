"""GET /api/v1/app/catalog over HTTP: the end-user app twin of the admin panel.

Reads `user_actions`, needs NO open-cap (any authenticated user sees their own app), and is still
filtered to the caller's own capabilities.
"""
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
                 "ADMIN_SERVICES": "file-service=http://file-service:8080",
                 "ADMIN_CATALOG_TTL": "0"}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()

    import app.api.v1.app_catalog as ac

    async def fake_build(targets, caps, fetch, field="user_actions"):
        from app.core.admin_catalog import build_catalog

        async def stub(_t):
            return {"user_actions": [
                {"id": "files.list", "label": "My files", "method": "GET",
                 "path": "/api/v1/files", "capability": "files.read"},
                {"id": "files.upload", "label": "Upload", "method": "POST",
                 "path": "/api/v1/files", "capability": "files.upload"},
            ]}
        return await build_catalog(targets, caps, stub, field=field)

    monkeypatch.setattr(ac, "build_catalog", fake_build)

    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    get_settings.cache_clear()


def _h(caps):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "u", "caps": caps, "type": "access", "iat": now,
                      "exp": now + dt.timedelta(minutes=5), "iss": "api-gateway",
                      "aud": "base-services"}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def test_app_catalog_needs_no_open_cap_just_a_valid_token(client):
    # No platform.admin here — any authenticated user may fetch their own app catalog.
    r = client.get("/api/v1/app/catalog", headers=_h(["files.read"]))
    assert r.status_code == 200


def test_no_token_is_401(client):
    assert client.get("/api/v1/app/catalog").status_code == 401


def test_catalog_is_filtered_to_the_callers_own_caps(client):
    r = client.get("/api/v1/app/catalog", headers=_h(["files.read"]))
    ids = {a["id"] for s in r.json()["services"] for a in s["actions"]}
    assert "files.list" in ids                       # holds files.read
    assert "files.upload" not in ids, "an action whose cap the user lacks must not appear"


def test_a_user_with_both_caps_sees_both(client):
    r = client.get("/api/v1/app/catalog", headers=_h(["files.read", "files.upload"]))
    ids = {a["id"] for s in r.json()["services"] for a in s["actions"]}
    assert ids == {"files.list", "files.upload"}
