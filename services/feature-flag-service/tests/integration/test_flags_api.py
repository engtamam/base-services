"""Integration: flags CRUD/evaluate/overrides/stale, config versioning, maintenance, gating."""
from __future__ import annotations

import datetime as dt
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
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


def _adm(caps=("platform.flags",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "admin", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _int():
    return {"X-Internal-Token": ITOKEN}


def test_flag_lifecycle_and_sticky_rollout(client):
    r = client.post("/api/v1/flags", json={"key": "new_ui", "enabled": True, "rollout_pct": 50}, headers=_adm())
    assert r.status_code == 201

    first = client.get("/internal/flags/new_ui?user=alice", headers=_int()).json()
    for _ in range(3):                                    # sticky across calls
        assert client.get("/internal/flags/new_ui?user=alice", headers=_int()).json()["enabled"] == first["enabled"]

    client.post("/api/v1/flags/new_ui/overrides",
                json={"scope": "user", "subject": "alice", "value": False}, headers=_adm())
    after = client.get("/internal/flags/new_ui?user=alice", headers=_int()).json()
    assert after["enabled"] is False and after["reason"] == "user_override"


def test_bulk_evaluate(client):
    client.post("/api/v1/flags", json={"key": "bulk_a", "enabled": True, "rollout_pct": 100}, headers=_adm())
    body = client.post("/internal/flags/evaluate", json={"user": "bob"}, headers=_int()).json()
    assert body["flags"]["bulk_a"]["enabled"] is True
    only = client.post("/internal/flags/evaluate", json={"user": "bob", "keys": ["bulk_a"]}, headers=_int()).json()
    assert list(only["flags"]) == ["bulk_a"]


def test_unknown_flag_fail_safe(client):
    r = client.get("/internal/flags/does_not_exist?user=x", headers=_int()).json()
    assert r["enabled"] is False and r["reason"] == "unknown_flag"


def test_archive_and_stale(client):
    client.post("/api/v1/flags", json={"key": "old_thing", "enabled": True}, headers=_adm())
    stale = client.get("/api/v1/flags/stale?days=0", headers=_adm())
    assert stale.status_code == 200 and "stale" in stale.json()
    assert client.delete("/api/v1/flags/old_thing", headers=_adm()).json()["archived"] is True
    keys = [f["key"] for f in client.get("/api/v1/flags", headers=_adm()).json()["flags"]]
    assert "old_thing" not in keys


def test_config_version_history_rollback(client):
    client.put("/api/v1/config/rate_limit", json={"value": 100}, headers=_adm())
    client.put("/api/v1/config/rate_limit", json={"value": 500}, headers=_adm())
    assert client.get("/api/v1/config/rate_limit", headers=_adm()).json()["value"] == 500
    hist = client.get("/api/v1/config/rate_limit/history", headers=_adm()).json()["history"]
    assert [h["version"] for h in hist] == [2, 1]
    client.post("/api/v1/config/rate_limit/rollback?version=1", headers=_adm())
    assert client.get("/internal/config/rate_limit", headers=_int()).json()["value"] == 100


def test_config_rollback_unknown_version(client):
    client.put("/api/v1/config/x", json={"value": 1}, headers=_adm())
    assert client.post("/api/v1/config/x/rollback?version=99", headers=_adm()).status_code == 404


def test_maintenance_window_and_partial(client):
    now = time.time()
    client.put("/api/v1/maintenance",
               json={"module": "global", "enabled": True, "message": "up", "start": now + 60, "end": now + 120},
               headers=_adm())
    assert client.get("/internal/maintenance", headers=_int()).json()["active"] is False   # scheduled
    client.put("/api/v1/maintenance",
               json={"module": "global", "enabled": True, "message": "up", "start": now - 10, "end": now + 60},
               headers=_adm())
    assert client.get("/internal/maintenance", headers=_int()).json()["active"] is True
    client.put("/api/v1/maintenance", json={"module": "global", "enabled": False}, headers=_adm())
    client.put("/api/v1/maintenance", json={"module": "reports", "enabled": True}, headers=_adm())
    assert client.get("/internal/maintenance?module=reports", headers=_int()).json()["active"] is True
    assert client.get("/internal/maintenance?module=other", headers=_int()).json()["active"] is False


def test_read_only_cap_cannot_write(client):
    ro = _adm(caps=("flags.read",))
    assert client.get("/api/v1/flags", headers=ro).status_code == 200
    assert client.post("/api/v1/flags", json={"key": "nope", "enabled": True}, headers=ro).status_code == 403
    assert client.put("/api/v1/config/z", json={"value": 1}, headers=ro).status_code == 403


def test_tenant_admin_cannot_override_another_tenant(client):
    client.post("/api/v1/flags", json={"key": "scoped", "enabled": True, "rollout_pct": 0}, headers=_adm())
    own = _adm(caps=("flags.manage",), tenant="tenant-A")
    client.post("/api/v1/flags/scoped/overrides",
                json={"scope": "tenant", "subject": "tenant-B", "value": True}, headers=own)
    flag = [f for f in client.get("/api/v1/flags", headers=_adm()).json()["flags"] if f["key"] == "scoped"][0]
    assert "tenant-B" not in flag["tenant_overrides"]      # forced to their own tenant
    assert flag["tenant_overrides"].get("tenant-A") is True


def test_bad_flag_key_rejected(client):
    assert client.post("/api/v1/flags", json={"key": "bad key!", "enabled": True},
                       headers=_adm()).status_code == 422
    assert client.post("/api/v1/flags", json={"key": "ok", "rollout_pct": 150},
                       headers=_adm()).status_code == 422
