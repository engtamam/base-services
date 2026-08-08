"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring — routes exist, are internal-token gated, refuse without a backup key, and complete
a real export→import→verify roundtrip of the authorization record (roles, policies, assignments).
The assignment's composite key ("tenant::subject") round-trips because it is carried as the row id.
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
                      BACKUP_KEK=backup_kek)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    _seed(c)
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _seed(c):
    from app.core.models import Assignment, Policy, Role
    from app.core.stores import AssignmentStore, PolicyStore, RoleStore
    ds = c.app.state.container.datastore

    async def seed():
        await RoleStore(ds).create(Role(id="r1", tenant="acme", name="owner", capabilities=["*"]))
        await PolicyStore(ds).create(Policy(id="p1", tenant="acme", effect="allow", action="read"))
        await AssignmentStore(ds).set(Assignment(subject="u1", tenant="acme", roles=["owner"]))
    asyncio.run(seed())


def _h():
    return {"X-Internal-Token": ITOKEN}


# ── authorisation ─────────────────────────────────────────────────────────────
def test_export_requires_the_internal_token(client):
    assert client.post("/internal/backup/export", json={}).status_code == 401


def test_import_requires_the_internal_token(client):
    assert client.post("/internal/backup/import", json={"archive": {}}).status_code == 401


# ── no key ────────────────────────────────────────────────────────────────────
def test_without_a_backup_key_export_refuses():
    c = _make_app(backup_kek="")
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        assert c.post("/internal/backup/export", headers=_h(), json={}).status_code == 503
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


# ── roundtrip ─────────────────────────────────────────────────────────────────
def test_export_then_import_restores_the_authorization_record(client):
    exp = client.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["format"] == "base-backup/v1"
    for coll in ("roles", "policies", "assignments"):
        assert archive["row_counts"][coll] >= 1
    assert "owner" not in archive["sealed"]   # sealed payload

    imp = client.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert imp.status_code == 200, imp.text
    assert imp.json()["skipped"] >= 3         # all rows present -> idempotent

    # The assignment restores under its composite key and reads back with its roles.
    st = client.app.state.container.datastore
    loop = asyncio.new_event_loop()
    row = loop.run_until_complete(st.get("assignments", "acme::u1"))
    assert row and row["roles"] == ["owner"]
    loop.close()


def test_verify_confirms_the_archive_restores(client):
    archive = client.post("/internal/backup/export", headers=_h(), json={}).json()
    r = client.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 3


def test_a_corrupt_archive_is_refused(client):
    archive = client.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = client.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
