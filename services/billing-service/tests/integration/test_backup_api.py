"""The backup endpoints, driven over HTTP the way backup-service will drive them.

The unit tests prove the export/import logic; this proves the wiring — that the routes exist, are
gated by the internal token, refuse without a backup key, and complete a real export→import
roundtrip through the running service.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40   # MUST match tests/conftest so the session-scoped app is not disturbed
KEK = "backup-key-for-tests"


def _make_app(backup_kek: str = KEK):
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      BACKUP_KEK=backup_kek)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    # Snapshot and restore the FULL environment: this test sets BACKUP_KEK and rebuilds settings,
    # and the shared session-scoped client in test_endpoints must not inherit either. A leaked env
    # var here manifested as a 401 three tests away — worth the belt and braces.
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _hdr():
    return {"X-Internal-Token": ITOKEN}


def _seed_a_plan(client):
    # Seed straight into the store: the /plans route is capability-gated (an operator JWT), which
    # is not what this test is about — it exercises the backup endpoints, which are internal-token
    # gated. A row is a row for the archive's purposes.
    import asyncio
    st = client.app.state.container.datastore
    asyncio.get_event_loop().run_until_complete(
        st.put("plans", "plan_pro", {"id": "plan_pro", "name": "Pro",
                                      "prices": [{"interval": "month", "amount_minor": 10000,
                                                  "currency": "SAR", "type": "flat"}],
                                      "active": True, "listed": True, "created": "2026-07-22"}))
    return "plan_pro"


# ── authorisation ─────────────────────────────────────────────────────────────
def test_export_requires_the_internal_token(client):
    assert client.post("/api/v1/../internal/backup/export", json={}).status_code in (401, 404)
    assert client.post("/internal/backup/export", json={}).status_code == 401


def test_import_requires_the_internal_token(client):
    assert client.post("/internal/backup/import", json={"archive": {}}).status_code == 401


# ── no key ────────────────────────────────────────────────────────────────────
def test_without_a_backup_key_export_refuses_rather_than_writing_in_the_clear():
    c = _make_app(backup_kek="")
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        r = c.post("/internal/backup/export", headers=_hdr(), json={})
        assert r.status_code == 503
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


# ── roundtrip ─────────────────────────────────────────────────────────────────
def test_export_then_import_restores_the_data(client):
    plan_id = _seed_a_plan(client)

    exp = client.post("/internal/backup/export", headers=_hdr(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["format"] == "base-backup/v1"
    assert archive["row_counts"]["plans"] >= 1
    # The payload is sealed — the plan name is not sitting in the response.
    assert "Pro" not in archive["sealed"]

    imp = client.post("/internal/backup/import", headers=_hdr(),
                      json={"archive": archive})
    assert imp.status_code == 200, imp.text
    # Idempotent: the plan already exists in this same store, so a re-import inserts nothing.
    assert imp.json()["skipped"] >= 1

    # The plan is still there and unchanged (read from the store; /plans is capability-gated).
    import asyncio
    st = client.app.state.container.datastore
    row = asyncio.get_event_loop().run_until_complete(st.get("plans", plan_id))
    assert row and row["name"] == "Pro"


def test_a_corrupt_archive_is_refused_with_a_reason(client):
    _seed_a_plan(client)
    archive = client.post("/internal/backup/export", headers=_hdr(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = client.post("/internal/backup/import", headers=_hdr(), json={"archive": archive})
    # 400, not 500: a bad archive is the caller's problem. The specific reason (checksum, key,
    # missing id) is logged, not echoed — the error middleware generalises every 4xx/5xx body.
    assert r.status_code == 400
