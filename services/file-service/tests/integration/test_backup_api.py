"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring and the two file-specific properties: the subject→file link restores under its
composite key (DSAR erasure depends on finding it), and the per-blob reference count is rebuilt from
the restored files — two files sharing one blob restore to a count of 2, so a later delete does not
wrongly GC a blob another file still references.
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
        # Two files sharing ONE content blob (sha256 "abc") -> the blob's ref-count is 2.
        await st.put("files", "f1", {"id": "f1", "tenant": "acme", "owner": "u1",
                                     "sha256": "abc", "storage_key": "abc", "size": 10})
        await st.put("files", "f2", {"id": "f2", "tenant": "acme", "owner": "u2",
                                     "sha256": "abc", "storage_key": "abc", "size": 10})
        await st.put("file_subjects", "acme|subj1|f1",
                     {"tenant": "acme", "subject_ref": "subj1", "file_id": "f1"})
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


def test_restore_rebuilds_links_and_blob_refs(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    assert archive["row_counts"]["files"] >= 2
    assert archive["row_counts"]["file_subjects"] >= 1
    assert "blob_refs" not in archive["row_counts"]        # derived, not archived

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text
        assert imp.json()["blob_refs_rebuilt"] >= 1

        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        # The subject link restored under its composite key — erasure can find it.
        assert loop.run_until_complete(
            st.get("file_subjects", "acme|subj1|f1"))["file_id"] == "f1"
        # Two files share the blob -> its ref-count came back as 2.
        assert loop.run_until_complete(st.get("blob_refs", "abc")) == {"count": 2}
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    body = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive}).json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 3


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    assert app_a.post("/internal/backup/import", headers=_h(),
                      json={"archive": archive}).status_code == 400
