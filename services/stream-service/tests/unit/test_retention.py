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
