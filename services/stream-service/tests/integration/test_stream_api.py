"""Integration: the stream HTTP API — register, start, playback, stop; auth + tenant isolation."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
           "STREAM_ENGINE_DRIVER": "null", "SESSION_PLACER_DRIVER": "local",
           "STREAM_ALLOW_PRIVATE": "true", "RATELIMIT_MAX": "1000",
           "RECORDING_SINK_DRIVER": "memory"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _h(caps=("stream.manage", "stream.view"), tenant="t1", sub="alice"):
    tok = jwt.encode({"sub": sub, "caps": list(caps), "tenant": tenant,
                      "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)}, SECRET,
                     algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


@pytest.fixture
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _register(c, url="rtsp://10.0.0.9:554/stream"):
    return c.post("/api/v1/sources", headers=_h(),
                  json={"name": "Front", "url": url, "kind": "rtsp"})


def test_full_flow_register_start_playback_stop(env_guard):
    c = _make_app()
    r = _register(c)
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["url"] == "rtsp://10.0.0.9:554"            # redacted, no path/creds
    assert "10.0.0.9:554/stream" not in r.text

    r = c.post("/api/v1/sessions", headers=_h(), json={"source_id": src["id"], "outputs": ["hls"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session"]["status"] == "live"
    assert "placement" not in body["session"]              # internal detail never exposed
    assert "token=" in body["playback"]["hls"]["playlist_url"]
    sid = body["session"]["id"]

    assert c.get(f"/api/v1/sessions/{sid}/playback", headers=_h()).status_code == 200
    r = c.post(f"/api/v1/sessions/{sid}/stop", headers=_h())
    assert r.status_code == 200 and r.json()["status"] == "stopped"


def test_register_blocks_ssrf(env_guard):
    c = _make_app(STREAM_ALLOW_PRIVATE="false")
    r = c.post("/api/v1/sources", headers=_h(),
               json={"name": "evil", "url": "http://169.254.169.254/latest"})
    assert r.status_code == 400


def test_quota_returns_429(env_guard):
    c = _make_app(STREAM_MAX_CONCURRENT_SESSIONS_PER_TENANT="1")
    src = _register(c).json()
    c.post("/api/v1/sessions", headers=_h(), json={"source_id": src["id"]})
    r = c.post("/api/v1/sessions", headers=_h(), json={"source_id": src["id"]})
    assert r.status_code == 429


def test_read_requires_capability(env_guard):
    c = _make_app()
    # A token with no stream caps cannot list sources.
    assert c.get("/api/v1/sources", headers=_h(caps=("other",))).status_code == 403


def test_tenant_isolation_hides_other_tenants_source(env_guard):
    c = _make_app()
    src = _register(c).json()
    assert c.get(f"/api/v1/sources/{src['id']}", headers=_h(tenant="t2")).status_code == 404


def test_delete_source_refuses_while_live_then_forces(env_guard):
    c = _make_app()
    src = _register(c).json()
    c.post("/api/v1/sessions", headers=_h(), json={"source_id": src["id"]})
    assert c.delete(f"/api/v1/sources/{src['id']}", headers=_h()).status_code == 409
    assert c.delete(f"/api/v1/sources/{src['id']}?force=true", headers=_h()).status_code == 200


def test_internal_sweep_and_heartbeat(env_guard):
    c = _make_app()
    INT = {"X-Internal-Token": "t" * 40}
    src = _register(c).json()
    sid = c.post("/api/v1/sessions", headers=_h(), json={"source_id": src["id"]}).json()["session"]["id"]
    # heartbeat extends the lease + records stats (internal token only)
    assert c.post(f"/internal/stream/sessions/{sid}/heartbeat", headers=INT,
                  json={"stats": {"bytes_out": 100}}).status_code == 200
    assert c.post(f"/internal/stream/sessions/{sid}/heartbeat", json={}).status_code == 401  # no token
    # sweep runs enforce+reconcile
    r = c.post("/internal/stream/sweep", headers=INT)
    assert r.status_code == 200 and "stopped" in r.json()
