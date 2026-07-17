"""FileService LARGE path — begin/complete/finalize, dedup, and the verification footguns."""
from __future__ import annotations

import hashlib

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.core.file_repository import FileRepository
from app.core.file_service import (
    BadDigest, FileService, LargeUploadUnavailable, TooLarge, TypeNotAllowed,
)
from app.core.upload_session import PENDING, QUARANTINED, READY, SCANNING, UploadSessionStore
from app.ports.object_store import CompletedPart
from tests.contracts.util import run
from tests.fakes import FakePresignStore

_REFS = "blob_refs"


def _svc(store, ds=None, *, max_object=5 << 30, part_size=8 << 20, allowed=frozenset()):
    ds = ds or MemoryDataStore()
    return FileService(
        repo=FileRepository(ds), object_store=store, scanner=None, signed_url=None,
        max_upload_bytes=25 << 20, allowed_content_types=allowed,
        sessions=UploadSessionStore(ds), queue=_FakeQueue(), scan_queue="file-scan",
        max_object_bytes=max_object, part_size=part_size,
    ), ds


class _FakeQueue:
    driver = "fake"

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    async def enqueue(self, queue, payload):
        self.jobs.append((queue, payload))
        return "j1"


def _upload(store, key, data):
    """Simulate the client's direct-to-bucket PUT."""
    run(store.put(key, data))


def test_large_path_unavailable_on_a_non_presign_store():
    svc, _ = _svc(MemoryObjectStore())
    assert svc.large_uploads_available is False
    with pytest.raises(LargeUploadUnavailable):
        run(svc.begin_large_upload(tenant="t", owner="u", filename="v.mp4",
                                   content_type="video/mp4", sha256="a" * 64, size=1 << 30))


def test_begin_rejects_a_bad_digest_and_oversize():
    svc, _ = _svc(FakePresignStore(), max_object=1 << 30)
    with pytest.raises(BadDigest):
        run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                   sha256="NOTHEX", size=10))
    with pytest.raises(TooLarge):
        run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                   sha256="a" * 64, size=(1 << 30) + 1))


def test_begin_rejects_a_disallowed_type():
    svc, _ = _svc(FakePresignStore(), allowed=frozenset({"video/mp4"}))
    with pytest.raises(TypeNotAllowed):
        run(svc.begin_large_upload(tenant="t", owner="u", filename="d", content_type="application/x-msdownload",
                                   sha256="a" * 64, size=10))


def test_happy_path_stages_verifies_promotes_and_refcounts():
    store = FakePresignStore()
    svc, ds = _svc(store)
    data = b"a-large-video" * 10_000
    sha = hashlib.sha256(data).hexdigest()

    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v.mp4",
                                       content_type="video/mp4", sha256=sha, size=len(data)))
    assert begin.deduped_file is None and begin.plan is not None
    sess = begin.session
    # Client uploads direct-to-bucket (simulated) onto the STAGING key, not the content key.
    _upload(store, sess.staging_key, data)
    assert run(store.exists(sess.staging_key)) and not run(store.exists(sha))

    parts = [CompletedPart(part_number=1, etag="e1")]
    after = run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u", parts=parts))
    assert after.status == SCANNING
    assert svc._queue.jobs == [("file-scan", {"session_id": sess.id})]

    final = run(svc.finalize_large_upload(sess.id, computed_sha=sha, clean=True))
    assert final.status == READY and final.file_id
    # promoted: content key now holds the bytes, staging is gone, ref-count is 1
    assert run(store.get(sha)) == data
    assert not run(store.exists(sess.staging_key))
    assert run(ds.get(_REFS, sha))["count"] == 1
    f = run(FileRepository(ds).get(final.file_id))
    assert f is not None and f.sha256 == sha and f.owner == "u"


def test_digest_mismatch_quarantines_and_never_touches_a_content_blob():
    store = FakePresignStore()
    svc, ds = _svc(store)
    data = b"honest-bytes" * 5000
    honest_sha = hashlib.sha256(data).hexdigest()
    lied_sha = "b" * 64                       # the client declares a different hash

    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                       sha256=lied_sha, size=len(data)))
    sess = begin.session
    _upload(store, sess.staging_key, data)
    run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))
    # worker computes the REAL hash, which does not match the declared key
    final = run(svc.finalize_large_upload(sess.id, computed_sha=honest_sha, clean=True))
    assert final.status == QUARANTINED and final.reason == "digest_mismatch"
    # the declared (victim-shaped) content key was NEVER written, and staging is cleaned up
    assert not run(store.exists(lied_sha))
    assert not run(store.exists(sess.staging_key))
    assert run(ds.get(_REFS, lied_sha)) is None


def test_scan_reject_quarantines():
    store = FakePresignStore()
    svc, ds = _svc(store)
    data = b"malware" * 1000
    sha = hashlib.sha256(data).hexdigest()
    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                       sha256=sha, size=len(data)))
    sess = begin.session
    _upload(store, sess.staging_key, data)
    run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))
    final = run(svc.finalize_large_upload(sess.id, computed_sha=sha, clean=False, reason="eicar"))
    assert final.status == QUARANTINED and final.reason == "eicar"
    assert not run(store.exists(sha)) and run(ds.get(_REFS, sha)) is None


def test_size_mismatch_at_complete_quarantines_before_scan():
    store = FakePresignStore()
    svc, ds = _svc(store)
    data = b"short"
    sha = hashlib.sha256(data).hexdigest()
    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                       sha256=sha, size=999_999))   # declared size lies
    sess = begin.session
    _upload(store, sess.staging_key, data)     # actual is 5 bytes
    after = run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u",
                                          parts=[CompletedPart(1, "e1")]))
    assert after.status == QUARANTINED and after.reason == "size_mismatch"
    assert svc._queue.jobs == []               # never handed to the scanner


def test_owner_scoped_dedup_short_circuits_with_no_upload():
    store = FakePresignStore()
    ds = MemoryDataStore()
    svc, _ = _svc(store, ds)
    data = b"already-have-this" * 1000
    sha = hashlib.sha256(data).hexdigest()
    # First upload the normal way so the owner demonstrably possesses it.
    b1 = run(svc.begin_large_upload(tenant="t", owner="u", filename="a.mp4", content_type="",
                                    sha256=sha, size=len(data)))
    _upload(store, b1.session.staging_key, data)
    run(svc.complete_large_upload(session_id=b1.session.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))
    run(svc.finalize_large_upload(b1.session.id, computed_sha=sha, clean=True))
    assert run(ds.get(_REFS, sha))["count"] == 1

    # Second time, same owner + same hash → instant dedup, no session, no upload, ref-count bumps.
    b2 = run(svc.begin_large_upload(tenant="t", owner="u", filename="copy.mp4", content_type="",
                                    sha256=sha, size=len(data)))
    assert b2.session is None and b2.plan is None and b2.deduped_file is not None
    assert run(ds.get(_REFS, sha))["count"] == 2


def test_dedup_does_not_short_circuit_for_a_different_owner():
    store = FakePresignStore()
    ds = MemoryDataStore()
    svc, _ = _svc(store, ds)
    data = b"owned-by-u" * 1000
    sha = hashlib.sha256(data).hexdigest()
    b1 = run(svc.begin_large_upload(tenant="t", owner="u", filename="a", content_type="",
                                    sha256=sha, size=len(data)))
    _upload(store, b1.session.staging_key, data)
    run(svc.complete_large_upload(session_id=b1.session.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))
    run(svc.finalize_large_upload(b1.session.id, computed_sha=sha, clean=True))

    # A DIFFERENT owner naming the same hash must NOT get a free reference — they must upload+verify.
    b2 = run(svc.begin_large_upload(tenant="t", owner="v", filename="steal", content_type="",
                                    sha256=sha, size=len(data)))
    assert b2.deduped_file is None and b2.session is not None


def test_complete_is_idempotent_on_retry():
    store = FakePresignStore()
    svc, _ = _svc(store)
    data = b"retry-me" * 2000
    sha = hashlib.sha256(data).hexdigest()
    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                       sha256=sha, size=len(data)))
    sess = begin.session
    _upload(store, sess.staging_key, data)
    run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u", parts=[CompletedPart(1, "e1")]))
    again = run(svc.complete_large_upload(session_id=sess.id, tenant="t", owner="u", parts=[CompletedPart(1, "e1")]))
    assert again.status == SCANNING
    assert len(svc._queue.jobs) == 1           # not enqueued twice


def test_cross_owner_session_access_is_hidden():
    from app.ports.errors import NotFound
    store = FakePresignStore()
    svc, _ = _svc(store)
    begin = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                       sha256="a" * 64, size=10))
    with pytest.raises(NotFound):
        run(svc.get_session(begin.session.id, tenant="t", owner="someone-else"))
