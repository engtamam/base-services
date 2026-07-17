"""stream-service ports — null engine, local placer (lease/reconcile), signed playback, sinks."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_adapters import DirectMediaEgress, NullPtzController, TurnMediaEgress
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.recording_sink import LocalRecordingSink, MemoryRecordingSink
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.ports.stream_types import EngineSpec


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _spec(tmp, outputs=("hls",)):
    return EngineSpec(source_url="rtsp://cam/1", transport="tcp", outputs=outputs,
                      output_dir=str(tmp))


def test_null_engine_lifecycle_and_snapshot(tmp_path):
    eng = NullStreamEngine()
    h = run(eng.start(_spec(tmp_path)))
    assert run(eng.is_alive(h))
    s1 = run(eng.stats(h)); s2 = run(eng.stats(h))
    assert s2.bytes_out > s1.bytes_out and s2.fps == 25.0     # synthetic throughput advances
    assert (tmp_path / "index.m3u8").exists()                  # HLS playlist written
    jpeg = run(eng.snapshot("rtsp://cam/1"))
    assert jpeg[:2] == b"\xff\xd8"                             # a real JPEG header
    assert run(eng.probe("rtsp://cam/1")).ok
    run(eng.stop(h))
    assert not run(eng.is_alive(h))
    assert run(eng.stats(h)).alive is False


def test_local_placer_places_and_stops(tmp_path):
    placer = LocalSessionPlacer(NullStreamEngine())
    p = run(placer.start("sess1", _spec(tmp_path)))
    assert p.placer == "local" and run(placer.is_alive(p))
    run(placer.stop(p))
    assert not run(placer.is_alive(p))


def test_reconcile_kills_orphans_and_reports_dead(tmp_path):
    placer = LocalSessionPlacer(NullStreamEngine())
    run(placer.start("live", _spec(tmp_path)))
    run(placer.start("orphan", _spec(tmp_path)))     # core no longer knows this one

    # core believes only {live, ghost} are sessions: 'orphan' must be killed, 'ghost' reported dead.
    res = run(placer.reconcile({"live", "ghost"}))
    assert len(res.killed_orphans) == 1              # orphan's engine reaped
    assert "ghost" in res.dead_sessions              # session with no engine here
    assert "live" not in res.dead_sessions


def test_signed_playback_roundtrip_expiry_and_tamper():
    sp = HmacSignedPlayback("secret" * 4)
    tok = sp.sign(tenant="t1", session_id="s1", output="hls", ttl=60)
    payload = sp.verify(tok)
    assert payload["tenant"] == "t1" and payload["output"] == "hls"

    assert sp.verify(tok + "x") is None              # tampered signature
    assert sp.verify("garbage") is None
    expired = sp.sign(tenant="t1", session_id="s1", output="hls", ttl=-1)
    assert sp.verify(expired) is None                # already expired


def test_signed_playback_needs_a_secret():
    with pytest.raises(ValueError):
        HmacSignedPlayback("")


def test_memory_and_local_sinks_store_and_delete(tmp_path):
    for sink in (MemoryRecordingSink(), LocalRecordingSink(str(tmp_path))):
        ref = run(sink.put_segment(tenant="t", session_id="s", seq=1, data=b"seg-bytes",
                                   content_type="video/mp4", start="a", end="b"))
        assert ref.bytes == 9
        assert run(sink.signed_url(ref.ref, ttl=60))
        assert run(sink.delete(ref.ref)) is True
        assert run(sink.delete(ref.ref)) is False


def test_media_egress_direct_vs_turn():
    assert DirectMediaEgress().ice_servers() == []
    turn = TurnMediaEgress(["turn:host:3478"], secret="s")
    assert turn.ice_servers()[0]["urls"] == ["turn:host:3478"]
    assert TurnMediaEgress([], secret="s").ice_servers() == []   # no urls → nothing advertised


def test_null_ptz_reports_no_capability():
    ptz = NullPtzController()
    cap = run(ptz.capability({}))
    assert cap.move is False and cap.presets == ()
    with pytest.raises(NotImplementedError):
        run(ptz.move({}, pan=0.1, tilt=0, zoom=0))
