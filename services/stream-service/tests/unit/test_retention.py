"""Retention — expired recordings deleted (segments released), legal holds exempt."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.recording_sink import MemoryRecordingSink
from app.core.models import Recording
from app.core.stores import RecordingStore
from tests.unit.test_stream_core import _svc


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Clock:
    def __init__(self): self.t = 4_000_000_000.0   # far future so 'days ago' is a real cutoff
    def __call__(self): return self.t


def test_enforce_retention_deletes_old_frees_segments_keeps_locked():
    svc, ds = _svc(clock=_Clock())
    recs = RecordingStore(ds)
    sink = MemoryRecordingSink()
    # seed a segment in the sink and reference it from an old recording
    seg = run(sink.put_segment(tenant="t", session_id="s", seq=1, data=b"x" * 100,
                               content_type="video/mp4", start="a", end="b"))
    run(recs.create(Recording(id="old", tenant="t", source_id="s", session_id="s",
                              started="2000-01-01T00:00:00+00:00", segments=[seg.__dict__])))
    run(recs.create(Recording(id="locked", tenant="t", source_id="s", session_id="s",
                              started="2000-01-01T00:00:00+00:00", locked=True)))
    run(recs.create(Recording(id="fresh", tenant="t", source_id="s", session_id="s",
                              started="3999-01-01T00:00:00+00:00")))

    res = run(svc.enforce_retention(days=30, recordings=recs, sink=sink))
    assert res["deleted_recordings"] == 1 and res["deleted_segments"] == 1
    assert run(recs.get("old")) is None
    assert run(recs.get("locked")) is not None      # legal hold exempt
    assert run(recs.get("fresh")) is not None       # too new
    assert sink.segments == {}                       # the old recording's bytes were released


def test_byte_budget_retention_evicts_oldest_past_the_cap_keeping_locked():
    # stream-004: with RETENTION_MAX_BYTES set, the NEWEST recordings up to the budget are kept and
    # everything older is evicted — even when all are within RETENTION_DAYS.
    svc, ds = _svc(clock=_Clock())
    recs = RecordingStore(ds)
    sink = MemoryRecordingSink()

    def _mk(rid, started, nbytes, locked=False):
        seg = run(sink.put_segment(tenant="t", session_id=rid, seq=1, data=b"x" * nbytes,
                                   content_type="video/mp4", start="a", end="b"))
        run(recs.create(Recording(id=rid, tenant="t", source_id="s", session_id=rid, started=started,
                                  total_bytes=nbytes, segments=[seg.__dict__], locked=locked)))

    # All fresh (within RETENTION_DAYS). Budget = 250 bytes.
    _mk("r_old", "3999-01-01T00:00:00+00:00", 100)      # oldest → evicted
    _mk("r_mid", "3999-06-01T00:00:00+00:00", 100)      # kept (100)
    _mk("r_new", "3999-12-01T00:00:00+00:00", 100)      # kept (200) — newest
    _mk("r_hold", "3990-01-01T00:00:00+00:00", 100, locked=True)   # legal hold, never counted/evicted

    res = run(svc.enforce_retention(days=30, recordings=recs, sink=sink, max_bytes=250))
    assert run(recs.get("r_new")) is not None and run(recs.get("r_mid")) is not None
    assert run(recs.get("r_old")) is None               # oldest beyond the 250-byte budget
    assert run(recs.get("r_hold")) is not None          # legal hold exempt from the byte budget
    assert res["deleted_recordings"] == 1
