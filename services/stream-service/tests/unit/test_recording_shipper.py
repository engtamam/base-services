"""RecordingShipper — ships completed segments, holds back the one being written, idempotent."""
from __future__ import annotations

import asyncio
import os

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.recording_sink import MemoryRecordingSink
from app.core.recording import RecordingShipper
from app.core.stores import RecordingStore


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seg(d, n, data=b"segment-bytes"):
    with open(os.path.join(d, f"rec_{n:08d}.mp4"), "wb") as fh:
        fh.write(data)


def _shipper():
    ds = MemoryDataStore()
    sink = MemoryRecordingSink()
    return RecordingShipper(sink=sink, recordings=RecordingStore(ds)), sink, RecordingStore(ds)


def test_ships_completed_holds_back_the_last_while_live(tmp_path):
    d = str(tmp_path)
    for n in range(3):
        _seg(d, n)
    shipper, sink, recs = _shipper()
    n = run(shipper.ship_session(tenant="t", session_id="s1", source_id="src",
                                 output_dir=d, final=False))
    assert n == 2                                   # 3 segments, newest held back (still being written)
    assert len(sink.segments) == 2
    # the newest file remains on disk
    assert os.path.exists(os.path.join(d, "rec_00000002.mp4"))
    rec = run(recs.get("s1"))
    assert rec is not None and len(rec.segments) == 2 and rec.total_bytes == 2 * len(b"segment-bytes")


def test_final_ships_the_last_segment_too(tmp_path):
    d = str(tmp_path)
    for n in range(2):
        _seg(d, n)
    shipper, sink, recs = _shipper()
    run(shipper.ship_session(tenant="t", session_id="s2", source_id="src", output_dir=d, final=False))
    n = run(shipper.ship_session(tenant="t", session_id="s2", source_id="src", output_dir=d, final=True))
    assert n == 1                                   # the held-back one ships on final
    rec = run(recs.get("s2"))
    assert len(rec.segments) == 2 and rec.ended != ""    # recording closed


def test_shipping_is_idempotent(tmp_path):
    d = str(tmp_path)
    for n in range(3):
        _seg(d, n)
    shipper, sink, recs = _shipper()
    run(shipper.ship_session(tenant="t", session_id="s3", source_id="src", output_dir=d))
    again = run(shipper.ship_session(tenant="t", session_id="s3", source_id="src", output_dir=d))
    assert again == 0                               # already shipped (local copies deleted)
    assert len(run(recs.get("s3")).segments) == 2


def test_empty_dir_ships_nothing(tmp_path):
    shipper, _, _ = _shipper()
    assert run(shipper.ship_session(tenant="t", session_id="s4", source_id="src",
                                    output_dir=str(tmp_path))) == 0


def test_segments_are_stamped_with_footage_time_not_ship_time(tmp_path):
    """stream-003: each segment's start/end must be its FOOTAGE time range (recording start +
    seq*duration), not start==end==the ship instant — otherwise export windowing selects nothing."""
    import datetime as _dt

    d = str(tmp_path)
    for n in range(3):
        _seg(d, n)
    ds = MemoryDataStore()
    recs = RecordingStore(ds)
    shipper = RecordingShipper(sink=MemoryRecordingSink(), recordings=recs,
                               record_segment_seconds=60)
    run(shipper.ship_session(tenant="t", session_id="s5", source_id="src", output_dir=d, final=True))
    rec = run(recs.get("s5"))
    segs = sorted(rec.segments, key=lambda s: s["seq"])
    # every segment has a real, non-empty range and start < end
    for s in segs:
        assert s["start"] and s["end"] and s["start"] < s["end"]
    # consecutive segments advance by exactly the segment duration and never overlap
    starts = [_dt.datetime.fromisoformat(s["start"]) for s in segs]
    assert (starts[1] - starts[0]) == _dt.timedelta(seconds=60)
    assert (starts[2] - starts[1]) == _dt.timedelta(seconds=60)
    # and seg[0].end == seg[1].start (contiguous half-open ranges)
    assert segs[0]["end"] == segs[1]["start"]
