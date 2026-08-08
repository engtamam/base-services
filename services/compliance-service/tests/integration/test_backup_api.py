"""The backup endpoints, driven over HTTP the way backup-service will drive them.

These are legal records, so the roundtrip proves the two things a regulator's evidence must survive:
consent restores under its CANONICAL key (the purpose is normalised, so a mixed-case purpose would
land under the wrong key without the stamped id), and the append-only proof's sequence counter is
rebuilt so the next proof cannot re-use a number.
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
def app_a():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    _seed(c)
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _seed(c):
    from app.core.models import Consent, ConsentProof
    from app.core.stores import ConsentStore
    store = ConsentStore(c.app.state.container.datastore)

    async def seed():
        # A MIXED-CASE purpose: the canonical key normalises it to "marketing", so the row's raw
        # "Marketing" cannot rebuild the key — only the stamped id can.
        await store.put(Consent(tenant="acme", subject="u1", purpose="Marketing"))
        await store.add_proof(ConsentProof(id="x", tenant="acme", subject="u1",
                                           purpose="Marketing", action="granted"))
        await store.add_proof(ConsentProof(id="y", tenant="acme", subject="u1",
                                           purpose="Marketing", action="withdrawn"))
    asyncio.run(seed())


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


# ── roundtrip into a FRESH store ────────────────────────────────────────────────
def test_restore_reconstructs_consent_key_and_proof_counter(app_a):
    exp = app_a.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["row_counts"]["consent"] >= 1
    assert archive["row_counts"]["consent_proof"] >= 2
    assert "consent_proof_seq" not in archive["row_counts"]   # derived, not archived

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text
        assert imp.json()["proof_seq_rebuilt"] >= 1

        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        # Consent restored under its CANONICAL key (normalised purpose), not the raw "Marketing".
        row = loop.run_until_complete(st.get("consent", "acme::u1::marketing"))
        assert row is not None and row["purpose"] == "Marketing"
        # The proof counter was rebuilt to the highest sequence, so the next proof is number 3.
        seq = loop.run_until_complete(st.get("consent_proof_seq", "acme::u1"))
        assert seq == {"n": 2}
        loop.close()

        # And a fresh proof appended after restore does NOT collide — it takes the next number.
        from app.core.models import ConsentProof
        from app.core.stores import ConsentStore
        loop = asyncio.new_event_loop()
        p = loop.run_until_complete(ConsentStore(st).add_proof(
            ConsentProof(id="z", tenant="acme", subject="u1", purpose="Marketing", action="granted")))
        assert p.id.startswith("000000000003::")
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    r = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 3


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = app_a.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
