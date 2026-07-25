"""The operator job-detail read — the see-before-you-act companion to replay and cancel."""
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
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _tok(caps, tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    t = jwt.encode({"sub": "op", "caps": list(caps), "tenant": tenant,
                    "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + t}


def _enqueue(client, tenant):
    r = client.post("/internal/jobs/enqueue", json={"type": "email", "tenant": tenant},
                    headers={"X-Internal-Token": INTERNAL})
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def test_an_operator_reads_a_jobs_detail(client):
    jid = _enqueue(client, "acme")
    r = client.get(f"/api/v1/jobs/{jid}", headers=_tok(["platform.jobs"], tenant="ops"))
    assert r.status_code == 200 and r.json()["id"] == jid


def test_detail_needs_a_capability(client):
    jid = _enqueue(client, "t1")
    assert client.get(f"/api/v1/jobs/{jid}").status_code == 401
    assert client.get(f"/api/v1/jobs/{jid}", headers=_tok(["unrelated"])).status_code == 403


def test_a_tenant_reader_cannot_see_another_tenants_job(client):
    jid = _enqueue(client, "owner")
    # jobs.read scoped to 'attacker' must not read 'owner's job — it reads as absent.
    r = client.get(f"/api/v1/jobs/{jid}", headers=_tok(["jobs.read"], tenant="attacker"))
    assert r.status_code == 404


def test_the_literal_routes_are_not_shadowed_by_the_detail_route(client):
    # /stats and /dead-letters must still resolve, not bind job_id="stats".
    assert client.get("/api/v1/jobs/stats", headers=_tok(["platform.jobs"])).status_code == 200
    assert client.get("/api/v1/jobs/dead-letters", headers=_tok(["platform.jobs"])).status_code == 200
