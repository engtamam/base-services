"""Integration: the LARGE, direct-to-bucket upload flow over HTTP.

The app is built with a memory store (no presign) to prove the fail-closed negotiation, then with a
FakePresignStore swapped into the container to drive the full initiate → (simulated PUT) → complete →
scan → download-redirect flow. The real S3 adapter is covered by the object-store contract on MinIO.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
INTERNAL = "t" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": INTERNAL,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
           "OBJECTSTORE_DRIVER": "memory", "SIGNED_URL_SECRET": "s" * 32,
           "RATELIMIT_MAX": "1000", "MAX_INLINE_BYTES": "1024"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _h(caps=("files.upload", "files.read"), tenant="t1", sub="alice"):
    tok = jwt.encode({"sub": sub, "caps": list(caps), "tenant": tenant,
                      "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)}, SECRET,
                     algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


@pytest.fixture
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def test_large_unavailable_returns_501_on_a_non_presign_store(env_guard):
    c = _make_app(OBJECTSTORE_DRIVER="memory")
    r = c.post("/api/v1/files/uploads", headers=_h(),
               json={"filename": "v.mp4", "content_type": "video/mp4",
                     "sha256": "a" * 64, "size": 10_000_000})
    assert r.status_code == 501   # body is a generic error code (CWE-209); discovery is via manifest


def test_inline_over_boundary_is_refused_before_buffering(env_guard):
    from tests.fakes import FakePresignStore
    c = _make_app()
    c.app.state.container.object_store = FakePresignStore()
    # A body over MAX_INLINE_BYTES (1024) is refused up front — never silently buffered inline.
    r = c.post("/api/v1/files", headers={**_h(), "Content-Type": "application/octet-stream",
                                         "Content-Length": "5000"}, content=b"x" * 5000)
    assert r.status_code == 413


def test_full_large_flow_initiate_complete_scan_download(env_guard):
    from tests.fakes import FakePresignStore
    c = _make_app()
    store = FakePresignStore()
    c.app.state.container.object_store = store

    data = b"a-large-clean-object" * 100_000
    sha = hashlib.sha256(data).hexdigest()

    # 1. initiate
    r = c.post("/api/v1/files/uploads", headers=_h(),
               json={"filename": "movie.bin", "content_type": "application/octet-stream",
                     "sha256": sha, "size": len(data)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["deduplicated"] is False
    sid = body["session"]["id"]
    assert body["upload"]["parts"] and "url" in body["upload"]["parts"][0]

    # 2. client uploads direct-to-bucket (simulated) onto the staging key
    from app.core.upload_session import UploadSession
    from tests.contracts.util import run
    staging = f"staging/{sid}"
    run(store.put(staging, data))

    # 3. complete → status scanning
    r = c.post(f"/api/v1/files/uploads/{sid}/complete", headers=_h(),
               json={"parts": [{"part_number": 1, "etag": "e1"}]})
    assert r.status_code == 200 and r.json()["status"] == "scanning"

    # 4. drain the scan queue (the CronJob's internal endpoint)
    r = c.post("/internal/files/scan", headers={"X-Internal-Token": INTERNAL})
    assert r.status_code == 200 and r.json()["processed"] == 1

    # 5. status is now ready with a file id
    r = c.get(f"/api/v1/files/uploads/{sid}", headers=_h())
    assert r.status_code == 200 and r.json()["status"] == "ready"
    fid = r.json()["file_id"]

    # 6. download redirects straight to the bucket (app out of the data path)
    r = c.get(f"/api/v1/files/{fid}", headers=_h(), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("memory://" + sha)


def test_completed_upload_that_lies_about_size_is_quarantined(env_guard):
    from tests.fakes import FakePresignStore
    from tests.contracts.util import run
    c = _make_app()
    store = FakePresignStore()
    c.app.state.container.object_store = store
    data = b"short-bytes"
    sha = hashlib.sha256(data).hexdigest()
    r = c.post("/api/v1/files/uploads", headers=_h(),
               json={"filename": "x", "content_type": "application/octet-stream",
                     "sha256": sha, "size": 10_000_000})   # declared size lies
    sid = r.json()["session"]["id"]
    run(store.put(f"staging/{sid}", data))
    r = c.post(f"/api/v1/files/uploads/{sid}/complete", headers=_h(),
               json={"parts": [{"part_number": 1, "etag": "e1"}]})
    assert r.json()["status"] == "quarantined" and r.json()["reason"] == "size_mismatch"


def test_upload_status_is_hidden_across_owners(env_guard):
    from tests.fakes import FakePresignStore
    c = _make_app()
    c.app.state.container.object_store = FakePresignStore()
    r = c.post("/api/v1/files/uploads", headers=_h(sub="alice"),
               json={"filename": "v", "content_type": "application/octet-stream",
                     "sha256": "a" * 64, "size": 5000})
    sid = r.json()["session"]["id"]
    # A different user must not see alice's session.
    r2 = c.get(f"/api/v1/files/uploads/{sid}", headers=_h(sub="mallory"))
    assert r2.status_code == 404
