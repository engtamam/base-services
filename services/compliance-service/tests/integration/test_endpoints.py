"""The three endpoint tiers respond correctly."""
from __future__ import annotations

from tests.conftest import INTERNAL_TOKEN


def test_privacy_erase_refuses_an_unindexed_subject_kind(client, monkeypatch):
    # Item 6: compliance is itself an erase target. Handed a subject_kind it does not index, it must
    # refuse explicitly (unrecognized_subject_kind) — never match nothing and ack a false "erased".
    from app import config
    monkeypatch.setenv("PRIVACY_INDEXED_SUBJECT_KINDS", "internal_id")
    monkeypatch.setenv("PII_SALT", "test-salt-0123456789")   # the accepted branch runs the real eraser
    config.get_settings.cache_clear()
    h = {"X-Internal-Token": INTERNAL_TOKEN}
    try:
        r = client.post("/internal/privacy/erase",
                        json={"tenant": "t1", "subject": "u_7", "subject_kind": "phone_hash"},
                        headers=h)
        assert r.status_code == 200 and r.json()["unrecognized_subject_kind"] is True
        ok = client.post("/internal/privacy/erase",
                         json={"tenant": "t1", "subject": "u_7", "subject_kind": "internal_id"},
                         headers=h)
        assert ok.status_code == 200 and "unrecognized_subject_kind" not in ok.json()
    finally:
        config.get_settings.cache_clear()


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
