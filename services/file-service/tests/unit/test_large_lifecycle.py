"""Large-object lifecycle: abort a session, sweep stale ones, and erase in-flight uploads."""
from __future__ import annotations

import hashlib

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import FileErasure
from app.core.file_repository import FileRepository
from app.core.file_service import FileService
from app.core.upload_session import ABORTED, PENDING, UploadSessionStore
from app.ports.errors import NotFound
from tests.contracts.util import run
from tests.fakes import FakePresignStore

_SESSIONS = "upload_sessions"


def _wire():
    ds = MemoryDataStore()
    store = FakePresignStore()
    svc = FileService(
        repo=FileRepository(ds), object_store=store, scanner=None, signed_url=None,
        max_upload_bytes=25 << 20, allowed_content_types=frozenset(),
        sessions=UploadSessionStore(ds), queue=_Q(), scan_queue="file-scan",
        max_object_bytes=5 << 30, part_size=8 << 20,
    )
    return svc, store, ds


class _Q:
    driver = "q"

    async def enqueue(self, *a):
        return "j"


def _begin(svc, store, owner="u", tenant="t"):
    data = b"video" * 10_000
    sha = hashlib.sha256(data).hexdigest()
    b = run(svc.begin_large_upload(tenant=tenant, owner=owner, filename="v", content_type="",
                                   sha256=sha, size=len(data)))
    run(store.put(b.session.staging_key, data))
    return b.session


def test_abort_releases_the_staging_object():
    svc, store, ds = _wire()
    sess = _begin(svc, store)
    assert run(store.exists(sess.staging_key))
    out = run(svc.abort_session(sess.id, tenant="t", owner="u"))
    assert out.status == ABORTED and out.reason == "aborted_by_owner"
    assert not run(store.exists(sess.staging_key))


def test_abort_is_owner_scoped():
    svc, store, ds = _wire()
    sess = _begin(svc, store, owner="u")
    with pytest.raises(NotFound):
        run(svc.abort_session(sess.id, tenant="t", owner="mallory"))


def test_sweep_aborts_only_stale_pending_sessions():
    svc, store, ds = _wire()
    stale = _begin(svc, store)
    # Backdate the stale session's created time in the store.
    row = run(ds.get(_SESSIONS, stale.id))
    row["created"] = "2000-01-01T00:00:00+00:00"
    run(ds.put(_SESSIONS, stale.id, row))
    fresh = _begin(svc, store)   # created ~now

    res = run(svc.sweep_stale_sessions("2025-01-01T00:00:00+00:00"))
    assert res["aborted"] == 1
    assert run(svc._sessions.get(stale.id)).status == ABORTED
    assert not run(store.exists(stale.staging_key))
    assert run(svc._sessions.get(fresh.id)).status == PENDING   # too new to sweep


def test_erasure_aborts_a_subjects_in_flight_uploads_and_reports_it():
    svc, store, ds = _wire()
    sess = _begin(svc, store, owner="alice", tenant="t")
    eraser = FileErasure(store=ds, object_store=store, pii_salt="salt")
    ack = run(eraser.erase("t", "alice"))
    assert any(e.startswith("upload_sessions:1") for e in ack["erased"])
    assert run(svc._sessions.get(sess.id)).status == ABORTED
    assert not run(store.exists(sess.staging_key))       # staging released, receipt is truthful


def test_erasure_of_a_subject_with_nothing_is_still_a_clean_ack():
    svc, store, ds = _wire()
    eraser = FileErasure(store=ds, object_store=store, pii_salt="salt")
    ack = run(eraser.erase("t", "nobody"))
    assert ack["already_absent"] is True and ack["erased"] == []
