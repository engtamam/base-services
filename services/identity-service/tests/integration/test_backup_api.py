"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring — routes exist, are internal-token gated, refuse without a backup key, and complete
a real export→import→verify roundtrip of accounts, memberships, and durable secrets. The credentials
ARE the data here, and the two login indexes are derived, so this also proves the reindex rebuilds
them from the restored accounts.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40   # MUST match tests/conftest's INTERNAL_SERVICE_TOKEN so the session app is undisturbed
KEK = "backup-key-for-tests"


@pytest.fixture
def client(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
                 "IDENTITY_MULTI_TENANT": "true", "RATELIMIT_MAX": "1000",
                 "BACKUP_KEK": KEK}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    _seed(c)
    yield c
    get_settings.cache_clear()


def _seed(c):
    from app.core.membership import Membership, MembershipStore, Person
    from app.core.models import User
    from app.core.user_repository import UserRepository
    cont = c.app.state.container
    ms = MembershipStore(cont.datastore)
    users = UserRepository(cont.datastore, cont.login_identifier)

    async def seed():
        # An account (creates users + users_by_username) and a cross-tenant person with a membership
        # (creates persons + persons_by_identifier + memberships).
        await users.create(User(id="u1", tenant="acme", username="boss@acme",
                                password_hash=cont.password_hasher.hash("x" * 12)))
        await ms.create_person(Person(id="p1", identifier="boss@acme",
                                      password_hash=cont.password_hasher.hash("x" * 12)))
        await ms.grant(Membership(person_id="p1", tenant="acme", roles=["owner"]))
    asyncio.run(seed())


def _h():
    return {"X-Internal-Token": ITOKEN}


# ── authorisation ─────────────────────────────────────────────────────────────
def test_export_requires_the_internal_token(client):
    assert client.post("/internal/backup/export", json={}).status_code == 401


def test_import_requires_the_internal_token(client):
    assert client.post("/internal/backup/import", json={"archive": {}}).status_code == 401


# ── no key ────────────────────────────────────────────────────────────────────
def test_without_a_backup_key_export_refuses(monkeypatch):
    for k, v in {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
                 "IDENTITY_MULTI_TENANT": "true", "BACKUP_KEK": ""}.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    try:
        assert c.post("/internal/backup/export", headers=_h(), json={}).status_code == 503
    finally:
        get_settings.cache_clear()


# ── roundtrip ─────────────────────────────────────────────────────────────────
def test_export_then_import_restores_accounts_and_rebuilds_indexes(client):
    exp = client.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["format"] == "base-backup/v1"
    # Durable collections are captured; the derived indexes are deliberately NOT in the archive.
    assert archive["row_counts"]["users"] >= 1
    assert archive["row_counts"]["memberships"] >= 1
    assert "users_by_username" not in archive["row_counts"]
    assert "persons_by_identifier" not in archive["row_counts"]
    # The payload is sealed — the identifier is not sitting in the response.
    assert "boss@acme" not in archive["sealed"]

    imp = client.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert imp.status_code == 200, imp.text
    body = imp.json()
    assert body["skipped"] >= 2                       # accounts already present -> idempotent
    assert body["reindexed"]["users_by_username"] >= 1
    assert body["reindexed"]["persons_by_identifier"] >= 1

    # The login lookups resolve after restore — proving the reindex reconstructed them.
    st = client.app.state.container.datastore
    loop = asyncio.new_event_loop()
    from app.core.user_repository import UserRepository
    users = UserRepository(st, client.app.state.container.login_identifier)
    assert loop.run_until_complete(users.get_by_username("acme", "boss@acme")) is not None
    assert loop.run_until_complete(st.get("persons_by_identifier", "boss@acme")) == {"id": "p1"}
    loop.close()


def test_import_rebuilds_lookups_after_they_were_lost(client):
    archive = client.post("/internal/backup/export", headers=_h(), json={}).json()
    st = client.app.state.container.datastore
    loop = asyncio.new_event_loop()
    # The exact restore scenario: accounts survive, derived lookups are gone.
    idx_key = f"acme::{client.app.state.container.login_identifier.normalize('boss@acme')}"
    loop.run_until_complete(st.delete("users_by_username", idx_key))
    loop.run_until_complete(st.delete("persons_by_identifier", "boss@acme"))

    imp = client.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert imp.status_code == 200, imp.text
    assert loop.run_until_complete(st.get("users_by_username", idx_key)) == {"id": "u1"}
    assert loop.run_until_complete(st.get("persons_by_identifier", "boss@acme")) == {"id": "p1"}
    loop.close()


def test_verify_confirms_the_archive_restores(client):
    archive = client.post("/internal/backup/export", headers=_h(), json={}).json()
    r = client.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 2


def test_a_corrupt_archive_is_refused(client):
    archive = client.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = client.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
