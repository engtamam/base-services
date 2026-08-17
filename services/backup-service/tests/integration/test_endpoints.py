"""The three endpoint tiers respond correctly."""
from __future__ import annotations

from tests.conftest import INTERNAL_TOKEN


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
