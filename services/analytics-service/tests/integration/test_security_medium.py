"""Security MEDIUM remediation: cross-tenant read audit coverage (analytics-008) and the
destructive-/internal boot guard (analytics-010)."""
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
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def _spy(client, monkeypatch):
    recs = []
    orig = client.app.state.auditor.record

    async def _rec(action, **kw):
        recs.append((action, kw))
        return await orig(action, **kw)

    monkeypatch.setattr(client.app.state.auditor, "record", _rec)
    return recs


# ── analytics-008: data-reading routes are audited like /query ──────────────────
def test_funnel_is_audited(client, monkeypatch):
    recs = _spy(client, monkeypatch)
    client.post("/api/v1/analytics/funnel",
                json={"steps": ["a", "b"], "start_ts": BASE, "end_ts": BASE + 60},
                headers=tok(["analytics.read"]))
    assert any(a == "analytics.queried" and kw.get("metadata", {}).get("kind") == "funnel"
               for a, kw in recs)


def test_retention_is_audited(client, monkeypatch):
    recs = _spy(client, monkeypatch)
    client.post("/api/v1/analytics/retention",
                json={"event_name": "hit", "cohort_start": BASE, "cohort_end": BASE + 60,
                      "periods": 2, "period_seconds": 86400},
                headers=tok(["analytics.read"]))
    assert any(a == "analytics.queried" and kw.get("metadata", {}).get("kind") == "retention"
               for a, kw in recs)


# ── analytics-010: destructive /internal routes fail-closed at boot in production ──
def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN=INTERNAL, PII_SALT="x" * 16)
    base.update(over)
    return Settings(**base)


def test_boot_guard_refuses_prod_with_destructive_routes_default_open():
    with pytest.raises(ValueError):
        _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()


def test_boot_guard_passes_when_destructive_routes_are_declared():
    pol = ('{"/internal/privacy/erase": ["compliance-service"], '
           '"/internal/backup/import": ["backup-service"], '
           '"/internal/analytics/prune-raw": ["*"]}')
    _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON=pol).assert_internal_policy_ok()


def test_boot_guard_is_noop_outside_production():
    _settings(ENV="test", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()
