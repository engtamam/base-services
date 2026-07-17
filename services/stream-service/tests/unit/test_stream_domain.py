"""stream-service domain — sealing/redaction, model serialization, stores (paging, quota count)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import LIVE, STOPPED, Guards, Recording, Session, Source, new_id
from app.core.sealing import Sealer, redact_url
from app.core.stores import RecordingStore, SessionStore, SourceStore, TriggerStore


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── sealing ──────────────────────────────────────────────────────────────────
def test_redact_strips_credentials_and_path():
    assert redact_url("rtsp://user:pass@cam.local:554/stream1") == "rtsp://cam.local:554"
    assert redact_url("http://h/x.m3u8") == "http://h"
    assert redact_url("not a url") == ""


def test_seal_roundtrip_and_ciphertext_hides_secret():
    s = Sealer("k" * 32)
    sealed = s.seal("rtsp://user:hunter2@cam/1")
    assert "hunter2" not in sealed and "user" not in sealed
    assert s.unseal(sealed) == "rtsp://user:hunter2@cam/1"


def test_seal_is_nondeterministic_but_both_unseal():
    s = Sealer("k" * 32)
    a, b = s.seal("secret"), s.seal("secret")
    assert a != b and s.unseal(a) == s.unseal(b) == "secret"


def test_sealer_requires_a_key():
    with pytest.raises(ValueError):
        Sealer("")


# ── models ───────────────────────────────────────────────────────────────────
def test_source_public_dict_never_leaks_sealed_or_credentials():
    src = Source(id="s1", tenant="t", name="Cam", url_sealed="SEALEDBLOB",
                 url_host="rtsp://cam:554")
    pub = str(src.public_dict())
    assert "SEALEDBLOB" not in pub and "url_sealed" not in pub
    assert src.public_dict()["url"] == "rtsp://cam:554"


def test_session_public_dict_hides_placement():
    sess = Session(id="x", tenant="t", source_id="s1", placement={"worker_id": "w9", "pid": 1234})
    pub = sess.public_dict()
    assert "placement" not in pub and "1234" not in str(pub)


def test_model_roundtrips():
    src = Source(id="s1", tenant="t", name="c", url_sealed="b", url_host="rtsp://h")
    assert Source.from_dict(src.to_dict()).to_dict() == src.to_dict()
    sess = Session(id="x", tenant="t", source_id="s1", guards=Guards(max_duration_s=100))
    assert Session.from_dict(sess.to_dict()).guards.max_duration_s == 100


# ── stores ───────────────────────────────────────────────────────────────────
def test_source_store_is_tenant_scoped():
    st = SourceStore(MemoryDataStore())
    run(st.create(Source(id=new_id(), tenant="t1", name="a", url_sealed="b", url_host="rtsp://h")))
    run(st.create(Source(id=new_id(), tenant="t2", name="b", url_sealed="b", url_host="rtsp://h")))
    rows, _ = run(st.list_for_tenant("t1"))
    assert len(rows) == 1 and rows[0].tenant == "t1"


def test_session_quota_count_is_bounded_and_counts_only_live():
    ds = MemoryDataStore()
    st = SessionStore(ds)
    for _ in range(5):
        run(st.create(Session(id=new_id(), tenant="t", source_id="s", status=LIVE)))
    run(st.create(Session(id=new_id(), tenant="t", source_id="s", status=STOPPED)))
    # cap=3 → reads at most 4, and a stopped session is never counted as live.
    assert run(st.count_live_for_tenant("t", cap=3)) == 4     # cap+1 ceiling proves boundedness
    assert run(st.count_live_for_tenant("t", cap=10)) == 5     # all live, stopped excluded


def test_recording_iter_expired_excludes_locked_and_future():
    ds = MemoryDataStore()
    st = RecordingStore(ds)
    run(st.create(Recording(id="old", tenant="t", source_id="s", session_id="x",
                            started="2000-01-01T00:00:00+00:00")))
    run(st.create(Recording(id="locked", tenant="t", source_id="s", session_id="x",
                            started="2000-01-01T00:00:00+00:00", locked=True)))
    run(st.create(Recording(id="fresh", tenant="t", source_id="s", session_id="x",
                            started="2999-01-01T00:00:00+00:00")))
    expired, _ = run(st.iter_expired("2025-01-01T00:00:00+00:00"))
    ids = {r.id for r in expired}
    assert ids == {"old"}       # locked exempt (legal hold), fresh too new


def test_trigger_store_roundtrip():
    st = TriggerStore(MemoryDataStore())
    run(st.put("src1", "t", [{"on": "motion", "do": ["record"]}]))
    assert run(st.get("src1"))[0]["on"] == "motion"
    assert run(st.get("missing")) == []
