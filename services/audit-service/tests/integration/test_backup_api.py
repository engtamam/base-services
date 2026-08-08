"""The backup endpoints, driven over HTTP the way backup-service will drive them.

This is the tamper-evident witness log, so the roundtrip proves the property that matters: after a
restore into a FRESH store, the hash chain still VERIFIES, and a new event appended afterwards
continues the chain (its seq does not collide, its hash links to the restored head). That only holds
if the events AND the per-partition chain head both came back — the head keyed by its partition.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40   # MUST match tests/conftest's INTERNAL_SERVICE_TOKEN so the session app is undisturbed
KEK = "backup-key-for-tests"


def _make_app(backup_kek: str = KEK) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      PII_SALT="test-pii-salt-not-a-real-one", BACKUP_KEK=backup_kek,
                      # A restore posts the whole sealed archive — the conftest's 2 KiB cap is for
                      # ordinary requests, not a backup body.
                      BODY_LIMIT_BYTES="5000000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def app_a():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    for i in range(3):
        assert c.post("/internal/audit", headers=_h(),
                      json={"service": "identity", "action": f"login.{i}",
                            "actor": "u1", "tenant": "acme"}).status_code in (200, 201)
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


# ── authorisation / no key ──────────────────────────────────────────────────────
def test_export_requires_the_internal_token(app_a):
    assert app_a.post("/internal/backup/export", json={}).status_code == 401


def test_without_a_backup_key_export_refuses():
    c = _make_app(backup_kek="")
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        assert c.post("/internal/backup/export", headers=_h(), json={}).status_code == 503
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


# ── roundtrip into a FRESH store, proven by chain verification ──────────────────
def test_restored_chain_verifies_and_continues(app_a):
    exp = app_a.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["row_counts"]["audit_events"] >= 3
    assert archive["row_counts"]["audit_meta"] >= 1        # the chain head came along

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text

        # The restored chain VERIFIES — every event still hash-links to the one before it. A broken
        # chain would 409; an intact one is 200.
        assert fresh.post("/internal/audit/verify", headers=_h()).status_code == 200

        # And a NEW event appended after the restore continues the chain (links to the restored head,
        # takes the next seq) — the head survived, keyed by its partition. Still verifies after.
        assert fresh.post("/internal/audit", headers=_h(),
                          json={"service": "identity", "action": "login.after",
                                "actor": "u1", "tenant": "acme"}).status_code in (200, 201)
        assert fresh.post("/internal/audit/verify", headers=_h()).status_code == 200
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    r = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 4       # >=3 events + >=1 meta


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = app_a.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
