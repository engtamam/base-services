"""FfmpegStreamEngine against the REAL ffmpeg binary — infra-gated (like the postgres/minio contracts).

Set STREAM_FFMPEG_PATH (or have `ffmpeg` on PATH) to run. It generates a short synthetic clip as the
"camera source", then proves probe/snapshot/HLS-segmentation/recording/stats/stop all work end to end.
Without ffmpeg the test SKIPS loudly — the same honesty rule as every other infra-gated adapter.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from app.adapters.stream.ffmpeg_engine import FfmpegStreamEngine
from app.ports.stream_types import EngineSpec

_FF = os.environ.get("STREAM_FFMPEG_PATH") or shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(not _FF, reason="no ffmpeg — set STREAM_FFMPEG_PATH to run")


_LOOP = asyncio.new_event_loop()


def run(coro):
    # ONE loop for the whole module: an asyncio subprocess is bound to the loop that created it, so
    # start()/stats()/stop() must share a loop (the same trap the postgres pool hit).
    return _LOOP.run_until_complete(coro)


@pytest.fixture
def clip(tmp_path):
    path = str(tmp_path / "cam.mp4")
    subprocess.run([_FF, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=1000", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path, "-y"],
                   check=True)
    return path


def test_probe_reports_real_stream_properties(clip):
    pr = run(FfmpegStreamEngine(_FF).probe(clip))
    assert pr.ok and pr.codec == "h264" and pr.width == 320 and pr.height == 240
    assert pr.fps == 15.0 and pr.has_audio is True


def test_snapshot_returns_a_real_jpeg(clip):
    jpg = run(FfmpegStreamEngine(_FF).snapshot(clip))
    assert jpg[:2] == b"\xff\xd8" and len(jpg) > 1000


def test_start_produces_hls_and_recording_then_stops_clean(clip, tmp_path):
    eng = FfmpegStreamEngine(_FF)
    out = str(tmp_path / "out")
    spec = EngineSpec(source_url=clip, transport="tcp", outputs=("hls", "record"),
                      output_dir=out, hls_segment_seconds=1, hls_window_segments=3,
                      record_segment_seconds=1)
    h = run(eng.start(spec))
    assert h.pid and run(eng.is_alive(h))
    run(asyncio.sleep(4))                       # transcode the whole 3s clip
    st = run(eng.stats(h))
    assert st.bytes_out > 0                       # real bytes were written
    run(eng.stop(h))
    assert not run(eng.is_alive(h))
    files = os.listdir(out)
    assert "index.m3u8" in files                  # HLS playlist
    assert any(f.startswith("rec_") for f in files)   # a recording segment landed


def test_real_segments_ship_to_the_sink_and_become_a_recording(clip, tmp_path):
    """End-to-end recording pipeline with REAL media: ffmpeg writes rec_ segments, the shipper moves
    them to the sink and records them. Connects the two independently-verified halves."""
    from app.adapters.datastore.memory import MemoryDataStore
    from app.adapters.stream.recording_sink import MemoryRecordingSink
    from app.core.recording import RecordingShipper
    from app.core.stores import RecordingStore

    eng = FfmpegStreamEngine(_FF)
    out = str(tmp_path / "rec")
    spec = EngineSpec(source_url=clip, transport="tcp", outputs=("record",),
                      output_dir=out, record_segment_seconds=1)
    h = run(eng.start(spec))
    run(asyncio.sleep(4))
    run(eng.stop(h))

    recs = RecordingStore(MemoryDataStore())
    sink = MemoryRecordingSink()
    shipper = RecordingShipper(sink=sink, recordings=recs)
    n = run(shipper.ship_session(tenant="t", session_id="sess1", source_id="src",
                                 output_dir=out, final=True))
    assert n >= 1                                 # at least one real segment shipped
    rec = run(recs.get("sess1"))
    assert rec is not None and len(rec.segments) == n and rec.total_bytes > 0
    assert len(sink.segments) == n                # bytes are in the sink
