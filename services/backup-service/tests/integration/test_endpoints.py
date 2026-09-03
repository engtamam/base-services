"""The three endpoint tiers respond correctly."""
from __future__ import annotations

import asyncio

from tests.conftest import INTERNAL_TOKEN

_H = {"X-Internal-Token": INTERNAL_TOKEN}


def _record_run(client, run: dict) -> None:
    from app.core.runs import RunStore
    ds = client.app.state.container.datastore
    asyncio.run(RunStore(ds).record(run))


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_ready_with_no_deps(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_metrics_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_internal_manifest_requires_token(client):
    assert client.get("/internal/manifest").status_code == 401
    assert client.get("/internal/health").status_code == 401


def test_internal_manifest_with_token(client):
    r = client.get("/internal/manifest", headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["name"]
    assert "endpoints" in body and "ports" in body and "events" in body


def test_internal_rejects_wrong_token(client):
    r = client.get("/internal/health", headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401


def test_backup_run_accepts_the_empty_post_its_cron_sends(client):
    # The nightly backup never ran because the run route required a body and the scheduled-job template
    # POSTs empty -> FastAPI 422 before the handler, invisible except in a Job exit code. It must accept
    # both an empty POST and no body at all.
    h = {"X-Internal-Token": INTERNAL_TOKEN}
    assert client.post("/internal/backup/run", headers=h).status_code == 200          # no body
    assert client.post("/internal/backup/run", json={}, headers=h).status_code == 200  # empty JSON


# ── tenant-coverage query (approach a: "was shop X verified restorable in run Y") ──
def test_tenant_coverage_requires_token_and_tenant(client):
    assert client.get("/internal/backup/tenant-coverage?tenant=acme").status_code == 401
    assert client.get("/internal/backup/tenant-coverage", headers=_H).status_code == 422


def test_tenant_coverage_404_when_no_run(client):
    assert client.get("/internal/backup/tenant-coverage?tenant=acme&run=nope", headers=_H).status_code == 404


def test_tenant_coverage_reports_covered_and_verified(client):
    _record_run(client, {
        "id": "run_cov1", "started_at": "2026-09-03T00:00:00Z", "status": "complete", "missed": [],
        "results": [
            {"service": "billing", "status": "ok", "verified": True, "tenants_covered": ["acme", "globex"]},
            {"service": "wallet", "status": "ok", "verified": True, "tenants_covered": ["acme"]},
            {"service": "audit", "status": "failed", "verified": False, "tenants_covered": ["acme"]},
        ],
    })
    r = client.get("/internal/backup/tenant-coverage?tenant=acme&run=run_cov1", headers=_H)
    assert r.status_code == 200
    b = r.json()
    assert b["covered"] is True
    assert b["covered_by"] == ["audit", "billing", "wallet"]
    assert b["verified_by"] == ["billing", "wallet"]
    assert b["unverified_by"] == ["audit"]        # covered but its restore FAILED
    assert b["fully_verified"] is False           # not every covering service verified
    assert b["run_incomplete"] is False

    # globex was only at billing, and that verified -> fully verified
    g = client.get("/internal/backup/tenant-coverage?tenant=globex&run=run_cov1", headers=_H).json()
    assert g["covered_by"] == ["billing"] and g["fully_verified"] is True

    # a tenant nobody covered
    n = client.get("/internal/backup/tenant-coverage?tenant=ghost&run=run_cov1", headers=_H).json()
    assert n["covered"] is False and n["fully_verified"] is False


def test_tenant_coverage_flags_an_incomplete_run(client):
    _record_run(client, {
        "id": "run_cov2", "started_at": "2026-09-03T01:00:00Z", "status": "incomplete",
        "missed": ["wallet"],
        "results": [{"service": "billing", "status": "ok", "verified": True, "tenants_covered": ["acme"]}],
    })
    b = client.get("/internal/backup/tenant-coverage?tenant=acme&run=run_cov2", headers=_H).json()
    # billing verified acme, but wallet was MISSED and may hold acme too — the answer must say so.
    assert b["fully_verified"] is True and b["run_incomplete"] is True
