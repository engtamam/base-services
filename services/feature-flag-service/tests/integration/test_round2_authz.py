"""Round-2 authz fixes (feature-flag-service): the tenant-delegatable flags.manage cap must NOT be
able to mutate the GLOBAL catalogue (flags, segments, versioned config, maintenance) — that is a
platform act requiring platform.flags. Maps to docs/audits/round2/feature-flag-service.json r2-01..04.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    yield TestClient(create_app(), raise_server_exceptions=False)
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _hdr(*caps, tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "u", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


# Each global-catalogue write: forbidden for flags.manage, allowed for platform.flags.
def test_flags_manage_cannot_mutate_global_catalogue(client):
    mng = _hdr("flags.manage")
    plat = _hdr("platform.flags")

    # r2-02: flag create/archive
    assert client.post("/api/v1/flags", json={"key": "k1", "enabled": True}, headers=mng).status_code == 403
    assert client.post("/api/v1/flags", json={"key": "k1", "enabled": True}, headers=plat).status_code == 201
    assert client.delete("/api/v1/flags/k1", headers=mng).status_code == 403
    assert client.delete("/api/v1/flags/k1", headers=plat).status_code == 200

    # r2-03: segment create/delete
    assert client.post("/api/v1/segments", json={"key": "s1", "rules": []}, headers=mng).status_code == 403
    assert client.post("/api/v1/segments", json={"key": "s1", "rules": []}, headers=plat).status_code == 201
    assert client.delete("/api/v1/segments/s1", headers=mng).status_code == 403

    # r2-04: versioned config write + rollback
    assert client.put("/api/v1/config/c1", json={"value": {"a": 1}}, headers=mng).status_code == 403
    assert client.put("/api/v1/config/c1", json={"value": {"a": 1}}, headers=plat).status_code == 200
    assert client.post("/api/v1/config/c1/rollback?version=1", headers=mng).status_code == 403

    # r2-01: global maintenance mode
    assert client.put("/api/v1/maintenance", json={"module": "global", "enabled": True},
                      headers=mng).status_code == 403
    assert client.put("/api/v1/maintenance", json={"module": "global", "enabled": True},
                      headers=plat).status_code in (200, 201)


def test_flags_manage_can_still_read(client):
    # flags.manage keeps READ access — the tightening is only on global-catalogue WRITES.
    assert client.get("/api/v1/flags", headers=_hdr("flags.manage")).status_code == 200
