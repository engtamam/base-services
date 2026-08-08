"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring — routes exist, are internal-token gated, refuse without a backup key, and complete
a real export→import→verify roundtrip. Rows are seeded straight into the store: this exercises the
backup seam, not the domain write paths, and a row is a row for the archive's purposes.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40   # MUST match tests/conftest's INTERNAL_SERVICE_TOKEN so the session app is undisturbed
KEK = "backup-key-for-tests"


def _make_app(backup_kek: str = KEK) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      PII_SALT="test-pii-salt-not-a-real-one", BACKUP_KEK=backup_kek,
                      BODY_LIMIT_BYTES="5000000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _seed(c):
    st = c.app.state.container.datastore
    async def seed():
        await st.put("verifications", "v1",
                     {"id": "v1", "tenant": "acme", "claim_type": "email", "status": "verified"})
        await st.put("identity_seen", "ref1", {"id": "ref1", "first_at": 1.0, "last_at": 1.0, "times": 1})
    asyncio.run(seed())


@pytest.fixture
def app_a():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    _seed(c)
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


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


def test_export_then_import_restores_into_a_fresh_store(app_a):
    exp = app_a.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["row_counts"]["verifications"] >= 1
    assert archive["row_counts"]["identity_seen"] >= 1

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text
        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        assert loop.run_until_complete(st.get("verifications", "v1"))["status"] == "verified"
        assert loop.run_until_complete(st.get("identity_seen", "ref1"))["times"] == 1
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    r = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 2


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = app_a.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
