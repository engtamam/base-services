"""WebRTC — engine WHIP output args, WHEP playback handle + ICE, viewer accounting.

The live media round-trip needs a WHIP/WHEP media server (mediamtx) and is infra-gated; the arg and
handle CONSTRUCTION (which is where mistakes hide) is verified here without one.
"""
from __future__ import annotations

from app.adapters.stream.ffmpeg_engine import FfmpegStreamEngine
from app.adapters.stream.null_adapters import TurnMediaEgress
from app.ports.stream_types import EngineSpec
from tests.unit.test_stream_core import _svc, _register, run


def test_engine_emits_whip_output_only_when_webrtc_requested():
    eng = FfmpegStreamEngine("ffmpeg")
    spec = EngineSpec(source_url="rtsp://cam/1", transport="tcp", outputs=("webrtc",),
                      output_dir="/tmp/x", webrtc_whip_url="http://mediamtx:8889/sess1/whip")
    args = eng.build_args(spec)
    assert "whip" in args and "http://mediamtx:8889/sess1/whip" in args
    assert "-tune" in args and "zerolatency" in args

    # No webrtc output → no whip in the argv.
    spec2 = EngineSpec(source_url="rtsp://cam/1", transport="tcp", outputs=("hls",),
                       output_dir="/tmp/x")
    assert "whip" not in eng.build_args(spec2)


def test_playback_returns_whep_url_and_ice_when_enabled():
    svc, _ = _svc(webrtc_enabled=True, webrtc_media_base="http://mediamtx:8889")
    svc._egress = TurnMediaEgress(["turn:t:3478"], secret="s")
    src = _register(svc)
    sess, playback = run(svc.start_session(tenant="t1", source_id=src.id,
                                           outputs=["hls", "webrtc"]))
    assert playback["webrtc"]["whep_url"] == f"http://mediamtx:8889/{sess.id}/whep"
    assert playback["webrtc"]["ice_servers"][0]["urls"] == ["turn:t:3478"]


def test_webrtc_omitted_when_not_enabled():
    svc, _ = _svc(webrtc_enabled=False)
    src = _register(svc)
    _, playback = run(svc.start_session(tenant="t1", source_id=src.id, outputs=["hls", "webrtc"]))
    assert "webrtc" not in playback           # disabled → not advertised (honest, not a broken URL)


def test_viewer_accounting_marks_a_viewer_on_playback_poll():
    svc, _ = _svc()
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    assert sess.viewers == 0
    run(svc.playback(sess.id, tenant="t1"))
    refreshed = run(svc.get_session(sess.id, tenant="t1"))
    assert refreshed.viewers >= 1 and refreshed.last_viewer_at   # a viewer is now counted
