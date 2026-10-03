"""Non-security MEDIUM remediation: query filters/unique/percentile coverage (analytics-011),
report_updated audit (analytics-002), export outcome accuracy (analytics-009), truncation warning (019)."""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40
BASE = 1_772_323_200.0


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      WAREHOUSE_DRIVER="csv", CLOCK_DRIVER="system", RATELIMIT_MAX="100000",
                      LATE_EVENT_MAX_SECONDS="315360000")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def tok(caps=("analytics.read",), tenant="t1"):
    claims = {"sub": "u1", "caps": list(caps), "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


IT = {"X-Internal-Token": INTERNAL}


def ingest(client, tenant, **body):
    body["tenant"] = tenant
    return client.post("/internal/analytics/ingest", json=body, headers=IT)


def _q(client, tenant, **q):
    q.setdefault("start_ts", BASE); q.setdefault("end_ts", BASE + 86400)
    return client.post("/api/v1/analytics/query", json=q, headers=tok(tenant=tenant)).json()


# ── analytics-011: a filter actually filters ──────────────────────────────────────
def test_filter_restricts_the_count(client):
    ingest(client, "t_f", id="f1", name="hit", event_time=BASE + 60, props={"plan": "pro"})
    ingest(client, "t_f", id="f2", name="hit", event_time=BASE + 61, props={"plan": "basic"})
    ingest(client, "t_f", id="f3", name="hit", event_time=BASE + 62, props={"plan": "pro"})
    r = _q(client, "t_f", metric="hit", agg="count", granularity="day",
           dimensions=["plan"], filters={"plan": "pro"})
    assert r.get("groups") == {"plan=pro": 2.0}, r      # only the matching cell, basic excluded


# ── analytics-011: unique counts distinct subjects ────────────────────────────────
def test_unique_counts_distinct_subjects(client):
    for i, subj in enumerate(["a", "a", "b"]):
        ingest(client, "t_u", id=f"u{i}", name="seen", event_time=BASE + 60 + i, subject=subj)
    r = _q(client, "t_u", metric="seen", agg="unique", granularity="day")
    assert r.get("total") == 2.0, r


# ── analytics-011: percentile returns a value ─────────────────────────────────────
def test_percentile_returns_a_value(client):
    for i, v in enumerate([10.0, 20.0, 30.0, 40.0]):
        ingest(client, "t_p", id=f"p{i}", name="lat", event_time=BASE + 60 + i, value=v)
    r = _q(client, "t_p", metric="lat", agg="percentile", percentile=0.5, granularity="day")
    assert "total" in r and r["total"] >= 10.0, r


# ── analytics-002: updating a report is audited ───────────────────────────────────
def test_update_report_is_audited(client, monkeypatch):
    recs = []
    orig = client.app.state.auditor.record

    async def _rec(action, **kw):
        recs.append(action)
        return await orig(action, **kw)

    h = tok(["platform.analytics"], tenant="t_rep")
    rid = client.post("/api/v1/analytics/reports",
                      json={"name": "r", "query": {"metric": "m", "start_ts": BASE, "end_ts": BASE + 86400}}, headers=h).json()["id"]
    monkeypatch.setattr(client.app.state.auditor, "record", _rec)
    client.put(f"/api/v1/analytics/reports/{rid}",
               json={"name": "r2", "query": {"metric": "m", "start_ts": BASE, "end_ts": BASE + 86400}}, headers=h)
    assert "analytics.report_updated" in recs
