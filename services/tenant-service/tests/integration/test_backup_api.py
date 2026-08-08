"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring — that the routes exist, are gated by the internal token, refuse without a backup
key, and complete a real export→import roundtrip of the tenant registry through the running service.
tenant-service is the root of every other service's tenancy, so a registry that does not round-trip
is the worst backup gap there is.
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
                      BASE_DOMAIN="apps.example.com", BACKUP_KEK=backup_kek)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    # Snapshot and restore the FULL environment: this test sets BACKUP_KEK and rebuilds settings,
    # and other modules' shared clients must not inherit it.
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _hdr():
    return {"X-Internal-Token": ITOKEN}


def _seed_a_tenant(client):
    # Seed straight into the store: /api/v1/tenants is capability-gated (a platform JWT), which is
    # not what this test is about — it exercises the internal-token-gated backup endpoints. Seed the
    # registry row AND its two indexes, exactly as the repository writes them, so the archive has
    # something in each declared collection.
    import asyncio
    st = client.app.state.container.datastore
    loop = asyncio.get_event_loop()
    loop.run_until_complete(st.put("tenants", "t_acme",
                                   {"id": "t_acme", "slug": "acme", "name": "Acme", "status": "active",
                                    "domains": ["acme.example.com"]}))
    loop.run_until_complete(st.put("tenant_slugs", "acme", {"id": "t_acme"}))
    loop.run_until_complete(st.put("tenant_domains", "acme.example.com", {"slug": "acme"}))
    return "t_acme"


# ── authorisation ─────────────────────────────────────────────────────────────
def test_export_requires_the_internal_token(client):
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
def test_export_then_import_restores_the_registry(client):
    _seed_a_tenant(client)

    exp = client.post("/internal/backup/export", headers=_hdr(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["format"] == "base-backup/v1"
    # The registry is captured. The two indexes are DERIVED and rebuilt on import — deliberately NOT
    # in the archive (they cannot round-trip through the id-keyed core; see backup_manifest).
    assert archive["row_counts"]["tenants"] >= 1
    assert "tenant_slugs" not in archive["row_counts"]
    assert "tenant_domains" not in archive["row_counts"]
    # The payload is sealed — the tenant name is not sitting in the response.
    assert "Acme" not in archive["sealed"]

    imp = client.post("/internal/backup/import", headers=_hdr(), json={"archive": archive})
    assert imp.status_code == 200, imp.text
    # Idempotent: the registry row already exists in this same store, so a re-import inserts nothing.
    assert imp.json()["skipped"] >= 1
    # The indexes were rebuilt from the registry.
    assert imp.json()["reindexed"] >= 1

    import asyncio
    st = client.app.state.container.datastore
    loop = asyncio.get_event_loop()
    row = loop.run_until_complete(st.get("tenants", "t_acme"))
    assert row and row["name"] == "Acme"
    # The slug/domain lookups resolve after the restore — the reindex reconstructed them.
    assert loop.run_until_complete(st.get("tenant_slugs", "acme")) == {"id": "t_acme"}
    assert loop.run_until_complete(st.get("tenant_domains", "acme.example.com")) == {"slug": "acme"}


def test_import_rebuilds_the_indexes_even_when_they_were_lost(client):
    # The exact restore scenario: the registry survives, the derived indexes are gone. Import must
    # bring the lookups back from the registry alone.
    import asyncio
    _seed_a_tenant(client)
    archive = client.post("/internal/backup/export", headers=_hdr(), json={}).json()
    st = client.app.state.container.datastore
    loop = asyncio.get_event_loop()
    loop.run_until_complete(st.delete("tenant_slugs", "acme"))
    loop.run_until_complete(st.delete("tenant_domains", "acme.example.com"))

    imp = client.post("/internal/backup/import", headers=_hdr(), json={"archive": archive})
    assert imp.status_code == 200, imp.text
    assert loop.run_until_complete(st.get("tenant_slugs", "acme")) == {"id": "t_acme"}
    assert loop.run_until_complete(st.get("tenant_domains", "acme.example.com")) == {"slug": "acme"}


def test_verify_confirms_the_archive_restores(client):
    _seed_a_tenant(client)
    archive = client.post("/internal/backup/export", headers=_hdr(), json={}).json()
    r = client.post("/internal/backup/verify", headers=_hdr(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 1


def test_a_corrupt_archive_is_refused_with_a_reason(client):
    _seed_a_tenant(client)
    archive = client.post("/internal/backup/export", headers=_hdr(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = client.post("/internal/backup/import", headers=_hdr(), json={"archive": archive})
    # 400, not 500: a bad archive is the caller's problem.
    assert r.status_code == 400
