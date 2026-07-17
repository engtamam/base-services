"""UploadSession model + store — state, serialization, keyset listing, stale-sweep."""
from __future__ import annotations

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.upload_session import (
    ABORTED, PENDING, READY, UploadSession, UploadSessionStore, new_id,
)
from tests.contracts.util import run


def _session(**kw) -> UploadSession:
    base = dict(id=new_id(), tenant="t1", owner="u1", filename="video.mp4",
                content_type="video/mp4", declared_sha256="a" * 64, declared_size=1 << 30,
                upload_id="mpu-1", part_size=8 << 20, part_count=128)
    base.update(kw)
    return UploadSession(**base)


def test_content_key_is_the_declared_hash_and_staging_is_per_session():
    s = _session(declared_sha256="b" * 64)
    assert s.content_key == "b" * 64
    assert s.staging_key == f"staging/{s.id}"   # per-session — a liar corrupts only its own staging


def test_terminal_states():
    assert _session(status=READY).is_terminal
    assert _session(status=ABORTED).is_terminal
    assert not _session(status=PENDING).is_terminal


def test_public_dict_never_leaks_the_multipart_id():
    s = _session(upload_id="secret-mpu")
    pub = s.public_dict()
    assert "secret-mpu" not in str(pub)
    assert "upload_id" not in pub
    assert pub["id"] == s.id and pub["status"] == PENDING


def test_roundtrip_serialization():
    s = _session(status=READY, file_id="f1")
    assert UploadSession.from_dict(s.to_dict()).to_dict() == s.to_dict()


def test_store_crud():
    store = UploadSessionStore(MemoryDataStore())
    s = _session()
    run(store.create(s))
    got = run(store.get(s.id))
    assert got is not None and got.status == PENDING
    got.status = READY
    run(store.save(got))
    assert run(store.get(s.id)).status == READY
    run(store.delete(s.id))
    assert run(store.get(s.id)) is None


def test_find_stale_pending_filters_by_age_and_status():
    store = UploadSessionStore(MemoryDataStore())
    old = _session(created="2020-01-01T00:00:00+00:00")
    fresh = _session(created="2999-01-01T00:00:00+00:00")
    ready_old = _session(status=READY, created="2020-01-01T00:00:00+00:00")
    for s in (old, fresh, ready_old):
        run(store.create(s))
    stale, _ = run(store.find_stale_pending("2025-01-01T00:00:00+00:00", limit=100))
    ids = {r["id"] for r in stale}
    assert old.id in ids
    assert fresh.id not in ids           # too new
    assert ready_old.id not in ids       # not pending — already terminal


def test_list_for_tenant_is_scoped():
    store = UploadSessionStore(MemoryDataStore())
    run(store.create(_session(tenant="t1")))
    run(store.create(_session(tenant="t2")))
    rows, _ = run(store.list_for_tenant("t1", limit=100))
    assert len(rows) == 1 and rows[0]["tenant"] == "t1"
