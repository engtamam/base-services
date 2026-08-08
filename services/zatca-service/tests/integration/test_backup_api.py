"""The backup endpoints, driven over HTTP the way backup-service will drive them.

These are tax records, so the roundtrip proves what a ZATCA restore must not get wrong: the invoices
and the sealed onboarding settings come back, and the ICV chain head is rebuilt so the NEXT invoice
cannot re-issue an ICV. reserved_icv must be the max across ALL invoices (including a reserved-but-
unsigned one), and last_hash must follow the highest SIGNED invoice.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40
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
        # Two SIGNED invoices (icv 1, 2) and one reserved-but-unsigned (icv 3, no hash).
        await st.put("zatca_invoices", "i1", {"id": "i1", "tenant": "acme", "number": "INV-1",
                                              "icv": 1, "hash": "h1", "status": "signed"})
        await st.put("zatca_invoices", "i2", {"id": "i2", "tenant": "acme", "number": "INV-2",
                                              "icv": 2, "hash": "h2", "status": "signed"})
        await st.put("zatca_invoices", "i3", {"id": "i3", "tenant": "acme", "number": "INV-3",
                                              "icv": 3, "hash": "", "status": "pending"})
        await st.put("zatca_settings", "acme", {"id": "acme", "enabled": True,
                                                "csid_cert_enc": "sealed-cert"})
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


def test_restore_rebuilds_the_icv_chain_head_and_number_index(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    assert archive["row_counts"]["zatca_invoices"] >= 3
    assert archive["row_counts"]["zatca_settings"] >= 1
    assert "zatca_index" not in archive["row_counts"]      # derived, not archived

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text
        assert imp.json()["index_rebuilt"]["heads"] == 1

        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        # The sealed onboarding settings restored under the tenant key.
        assert loop.run_until_complete(st.get("zatca_settings", "acme"))["csid_cert_enc"] == "sealed-cert"
        # The number lookup was rebuilt.
        assert loop.run_until_complete(st.get("zatca_index", "num::acme::INV-2")) == {"id": "i2"}
        # The chain head: reserved_icv is the MAX (3, incl. the reserved unsigned invoice), and
        # last_hash follows the highest SIGNED invoice (icv 2 -> "h2"), not the unsigned icv 3.
        head = loop.run_until_complete(st.get("zatca_index", "head::acme"))
        assert head == {"reserved_icv": 3, "last_hash": "h2"}
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    body = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive}).json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 4


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    assert app_a.post("/internal/backup/import", headers=_h(),
                      json={"archive": archive}).status_code == 400
