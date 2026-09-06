"""GET /internal/assistant-catalog — the UNFILTERED, internal-tier catalog for a KB ingest.

Two properties that separate it from the user-facing catalogs: it needs the internal token (not a
user JWT), and it returns EVERY action (user_actions + admin_actions merged) regardless of any
caller's capabilities — the assistant filters per-asking-user later.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
                 "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "s" * 48,
                 "ADMIN_SERVICES": "file-service=http://file-service:8080"}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()

    import app.core.admin_catalog as acat
    real = acat.build_catalog          # capture before patching to avoid recursion

    async def fake_build(targets, caps, fetch, field="admin_actions"):
        async def stub(_t):
            return {
                "user_actions": [
                    {"id": "files.list", "label": "My files", "method": "GET",
                     "path": "/api/v1/files", "capability": "files.read", "group": "files"}],
                "admin_actions": [
                    {"id": "files.purge", "label": "Purge", "method": "DELETE",
                     "path": "/api/v1/admin/files", "capability": "platform.files", "group": "admin"}],
            }
        return await real(targets, caps, stub, field=field)

    monkeypatch.setattr(acat, "build_catalog", fake_build)

    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    get_settings.cache_clear()


def _internal():
    return {"X-Internal-Token": "t" * 40}


def test_needs_the_internal_token_not_a_user_jwt(client):
    assert client.get("/internal/assistant-catalog").status_code == 401


def test_merges_user_and_admin_actions_unfiltered(client):
    r = client.get("/internal/assistant-catalog", headers=_internal())
    assert r.status_code == 200, r.text
    body = r.json()
    svc = next(s for s in body["services"] if s["service"] == "file-service")
    ids = {a["id"] for a in svc["actions"]}
    # BOTH the user action and the admin action appear — no caller-cap filtering at all.
    assert ids == {"files.list", "files.purge"}
    # every action keeps its capability (the assistant maps it to `requires` and filters later)
    caps = {a["id"]: a["capability"] for a in svc["actions"]}
    assert caps == {"files.list": "files.read", "files.purge": "platform.files"}


def test_shape_is_services_with_actions(client):
    body = client.get("/internal/assistant-catalog", headers=_internal()).json()
    assert "services" in body and "unreachable" in body
    for s in body["services"]:
        assert set(s) >= {"service", "actions"}
