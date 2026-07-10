"""API tier: deny-by-default RBAC, tenant isolation, ingest idempotency, export.

The app fixture is built per-module with its own env so it cannot pollute the shared client.
"""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40
BASE = 1_772_323_200.0          # 2026-03-01T00:00:00Z


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      WAREHOUSE_DRIVER="csv", CLOCK_DRIVER="system", RATELIMIT_MAX="100000",
                      LATE_EVENT_MAX_SECONDS="315360000")   # tests use fixed past timestamps
    get_settings.cache_clear()
    try:
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


IT = {"X-Internal-Token": INTERNAL}


def ingest(client, **body):
    body.setdefault("tenant", "t1")
    return client.post("/internal/analytics/ingest", json=body, headers=IT)


# ── deny-by-default ─────────────────────────────────────────────────────────
def test_query_without_a_token_is_denied(client):
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "m", "start_ts": BASE, "end_ts": BASE + 1})
    assert r.status_code == 401


def test_query_with_an_unrelated_capability_is_denied(client):
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "m", "start_ts": BASE, "end_ts": BASE + 1},
                    headers=tok(["billing.read"]))
    assert r.status_code == 403


def test_ingest_requires_the_internal_token(client):
    assert client.post("/internal/analytics/ingest",
                       json={"id": "x", "name": "n", "tenant": "t1"}).status_code == 401
    assert client.post("/internal/analytics/ingest", json={"id": "x", "name": "n", "tenant": "t1"},
                       headers={"X-Internal-Token": "w" * 40}).status_code == 401


def test_a_user_token_cannot_reach_the_internal_tier(client):
    r = client.post("/internal/analytics/ingest", json={"id": "x", "name": "n", "tenant": "t1"},
                    headers=tok(["platform.analytics"]))
    assert r.status_code == 401


def test_read_capability_cannot_manage_reports(client):
    r = client.post("/api/v1/analytics/reports",
                    json={"name": "r", "query": {"metric": "m", "start_ts": BASE,
                                                 "end_ts": BASE + 1}},
                    headers=tok(["analytics.read"]))
    assert r.status_code == 403


# ── ingest ──────────────────────────────────────────────────────────────────
def test_ingest_is_idempotent_over_http(client):
    body = {"id": "idem_1", "name": "sale", "tenant": "t_idem", "event_time": BASE + 60,
            "value": 99.0}
    assert ingest(client, **body).json()["duplicate"] is False
    for _ in range(4):
        assert ingest(client, **body).json()["duplicate"] is True

    r = client.post("/api/v1/analytics/query",
                    json={"metric": "sale", "agg": "sum", "granularity": "day",
                          "start_ts": BASE, "end_ts": BASE + 86400},
                    headers=tok(["platform.analytics"], tenant="t_idem"))
    assert r.json()["total"] == 99.0, "redelivery double-counted revenue"


def test_batch_ingest_reports_duplicates(client):
    events = [{"id": f"b{i}", "name": "hit", "event_time": BASE + 60} for i in range(5)]
    r1 = client.post("/internal/analytics/ingest/batch",
                     json={"tenant": "t_batch", "events": events}, headers=IT)
    assert r1.json() == {"accepted": 5, "duplicates": 0}
    r2 = client.post("/internal/analytics/ingest/batch",
                     json={"tenant": "t_batch", "events": events}, headers=IT)
    assert r2.json() == {"accepted": 0, "duplicates": 5}


def test_batch_ingest_rejects_an_event_without_an_id(client):
    r = client.post("/internal/analytics/ingest/batch",
                    json={"tenant": "t1", "events": [{"name": "hit"}]}, headers=IT)
    assert r.status_code == 400


def test_pii_never_becomes_a_dimension_over_http(client):
    r = ingest(client, id="pii_1", name="hit", tenant="t_pii", event_time=BASE + 60,
               props={"email": "a@b.c", "plan": "pro"})
    assert r.json()["dimensions"] == {"plan": "pro"}


# ── query ───────────────────────────────────────────────────────────────────
def test_query_is_cached_and_invalidated_by_a_write(client):
    q = {"metric": "cq", "agg": "count", "granularity": "day", "start_ts": BASE,
         "end_ts": BASE + 86400}
    h = tok(["analytics.read"], tenant="t_cache")
    ingest(client, id="cq1", name="cq", tenant="t_cache", event_time=BASE + 60)

    assert client.post("/api/v1/analytics/query", json=q, headers=h).json()["cached"] is False
    assert client.post("/api/v1/analytics/query", json=q, headers=h).json()["cached"] is True

    ingest(client, id="cq2", name="cq", tenant="t_cache", event_time=BASE + 60)
    r = client.post("/api/v1/analytics/query", json=q, headers=h).json()
    assert r["cached"] is False and r["total"] == 2.0


def test_query_rejects_an_unmaterialised_granularity(client):
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "m", "granularity": "minute", "start_ts": BASE,
                          "end_ts": BASE + 60}, headers=tok(["analytics.read"]))
    assert r.status_code == 400


def test_query_rejects_a_range_over_the_cost_limit(client):
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "m", "start_ts": 0, "end_ts": BASE},
                    headers=tok(["analytics.read"]))
    assert r.status_code == 400


# ── tenant isolation (CWE-639) ──────────────────────────────────────────────
def test_a_tenant_cannot_query_another_tenants_metrics(client):
    ingest(client, id="iso_1", name="secret", tenant="victim", event_time=BASE + 60)
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "secret", "granularity": "day", "start_ts": BASE,
                          "end_ts": BASE + 86400, "tenant": "victim"},
                    headers=tok(["analytics.read"], tenant="attacker"))
    assert r.status_code == 403


def test_an_attacker_querying_its_own_tenant_sees_nothing_of_the_victims(client):
    ingest(client, id="iso_2", name="secret", tenant="victim2", event_time=BASE + 60)
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "secret", "granularity": "day", "start_ts": BASE,
                          "end_ts": BASE + 86400},
                    headers=tok(["analytics.read"], tenant="attacker"))
    assert r.json()["total"] == 0.0


def test_platform_capability_may_target_any_tenant(client):
    ingest(client, id="plat_1", name="x", tenant="acme", event_time=BASE + 60)
    r = client.post("/api/v1/analytics/query",
                    json={"metric": "x", "granularity": "day", "start_ts": BASE,
                          "end_ts": BASE + 86400, "tenant": "acme"},
                    headers=tok(["platform.analytics"], tenant="ops"))
    assert r.status_code == 200 and r.json()["total"] == 1.0


# ── saved reports ───────────────────────────────────────────────────────────
def test_report_lifecycle_and_cross_tenant_read_is_a_404(client):
    q = {"metric": "rep", "agg": "count", "granularity": "day", "start_ts": BASE,
         "end_ts": BASE + 86400}
    ingest(client, id="rep_1", name="rep", tenant="t_rep", event_time=BASE + 60)
    h = tok(["analytics.manage"], tenant="t_rep")

    rid = client.post("/api/v1/analytics/reports", json={"name": "daily", "query": q},
                      headers=h).json()["id"]
    assert client.get(f"/api/v1/analytics/reports/{rid}", headers=h).status_code == 200
    assert client.post(f"/api/v1/analytics/reports/{rid}/run", headers=h).json()["total"] == 1.0
    assert client.get("/api/v1/analytics/reports", headers=h).json()["count"] >= 1

    other = tok(["analytics.manage"], tenant="t_other")
    assert client.get(f"/api/v1/analytics/reports/{rid}", headers=other).status_code == 404
    assert client.delete(f"/api/v1/analytics/reports/{rid}", headers=other).status_code == 404

    assert client.delete(f"/api/v1/analytics/reports/{rid}", headers=h).status_code == 204
    assert client.get(f"/api/v1/analytics/reports/{rid}", headers=h).status_code == 404


# ── export ──────────────────────────────────────────────────────────────────
def test_export_returns_csv(client):
    ingest(client, id="exp_1", name="exp", tenant="t_exp", event_time=BASE + 60)
    r = client.post("/api/v1/analytics/export",
                    json={"metric": "exp", "granularity": "day", "start_ts": BASE,
                          "end_ts": BASE + 86400},
                    headers=tok(["analytics.read"], tenant="t_exp"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "value" in r.text


# ── manifest ────────────────────────────────────────────────────────────────
def test_manifest_advertises_capabilities_and_ports(client):
    m = client.get("/internal/manifest", headers=IT).json()
    assert set(m["capabilities"]) == {"platform.analytics", "analytics.manage", "analytics.read"}
    assert {"port": "warehouse", "driver": "csv"} in m["ports"]
    assert any(e["path"] == "/internal/analytics/ingest" for e in m["endpoints"])
