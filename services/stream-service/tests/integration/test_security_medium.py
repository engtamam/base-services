"""Security MEDIUM remediation: PTZ rate-limit (stream-010) and export source-ownership (stream-012)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": SECRET, "STREAM_ENGINE_DRIVER": "null", "SESSION_PLACER_DRIVER": "local",
           "STREAM_ALLOW_PRIVATE": "true", "RATELIMIT_MAX": "1000", "RECORDING_SINK_DRIVER": "memory",
           "EXPORT_ENABLED": "true"}
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
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


# ── stream-012: an export for another tenant's source is refused ──────────────────
def test_export_rejects_another_tenants_source(env_guard):
    c = _make_app()
    src = c.post("/api/v1/sources", headers=_h(tenant="t1"),
                 json={"name": "Front", "url": "rtsp://10.0.0.9:554/s", "kind": "rtsp"}).json()
    # t2 tries to export t1's source id
    r = c.post("/api/v1/exports", headers=_h(tenant="t2"),
               json={"source_id": src["id"], "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:10Z", "format": "mp4"})
    assert r.status_code == 404                           # IDOR closed (stream-012)


def test_export_own_source_is_accepted(env_guard):
    c = _make_app()
    src = c.post("/api/v1/sources", headers=_h(tenant="t1"),
                 json={"name": "Front", "url": "rtsp://10.0.0.9:554/s", "kind": "rtsp"}).json()
    r = c.post("/api/v1/exports", headers=_h(tenant="t1"),
               json={"source_id": src["id"], "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:10Z", "format": "mp4"})
    assert r.status_code == 201, r.text


# ── stream-010: PTZ routes carry the rate-limit dependency ────────────────────────
def test_ptz_routes_are_rate_limited(env_guard):
    from app.api.ratelimit import rate_limit
    from app.api.v1.stream import router
    want = {"/sources/{source_id}/ptz/move", "/sources/{source_id}/ptz/stop",
            "/sources/{source_id}/ptz/goto/{preset_token}", "/sources/{source_id}/ptz/presets"}
    for r in router.routes:
        if getattr(r, "path", "") in want:
            deps = [d.dependency for d in getattr(r, "dependencies", [])]
            assert rate_limit in deps, f"{r.path} not rate-limited"
