"""API regressions for job-service audit findings #01 (schedules IDOR) and #04 (stats leak)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
INTERNAL = "i" * 40


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    yield TestClient(create_app(), raise_server_exceptions=False)
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _tok(caps, tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    t = jwt.encode({"sub": "op", "caps": list(caps), "tenant": tenant,
                    "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + t}


def _mk_schedule(client, tenant):
    r = client.post("/api/v1/jobs/schedules",
                    json={"name": f"s-{tenant}", "cron": "* * * * *", "type": "x"},
                    headers=_tok(["jobs.manage"], tenant=tenant))
    assert r.status_code == 201, r.text


def test_list_schedules_is_tenant_scoped(client):
    _mk_schedule(client, "acme")
    _mk_schedule(client, "globex")
    # a tenant reader sees ONLY its own schedules (was: every tenant's — cross-tenant IDOR)
    got = client.get("/api/v1/jobs/schedules", headers=_tok(["jobs.read"], tenant="acme")).json()["schedules"]
    assert got and all(s.get("tenant") == "acme" for s in got)
    # a platform operator sees across tenants
    allsch = client.get("/api/v1/jobs/schedules",
                        headers=_tok(["platform.jobs"], tenant="ops")).json()["schedules"]
    assert {"acme", "globex"} <= {s.get("tenant") for s in allsch}


def _enqueue(client, tenant):
    r = client.post("/internal/jobs/enqueue", json={"type": "email", "tenant": tenant},
                    headers={"X-Internal-Token": INTERNAL})
    assert r.status_code == 201, r.text


def test_stats_is_tenant_scoped_for_a_tenant_reader(client):
    _enqueue(client, "acme")
    _enqueue(client, "acme")
    _enqueue(client, "otherco")
    tenant_stats = client.get("/api/v1/jobs/stats", headers=_tok(["jobs.read"], tenant="acme")).json()
    plat_stats = client.get("/api/v1/jobs/stats", headers=_tok(["platform.jobs"], tenant="ops")).json()
    # the tenant reader's queued count is only its own; the platform view counts more (other tenants)
    assert plat_stats["by_status"].get("queued", 0) > tenant_stats["by_status"].get("queued", 0)
