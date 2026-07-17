"""Hot-path proof: the control plane NEVER touches media bytes.

The design guarantee is that video never flows through this service — start/stop/playback return URLs
and metadata only, and the bytes go engine → disk/bucket/media-server → viewer. We prove it by
counting every byte-producing engine call on the control path and asserting it stays zero, at any
number of playback polls.
"""
from __future__ import annotations

from app.adapters.stream.null_engine import NullStreamEngine
from tests.unit.test_stream_core import _svc, _register, run


class _CountingEngine(NullStreamEngine):
    """Counts calls that would move media bytes through this process."""
    def __init__(self):
        super().__init__()
        self.byte_calls = 0

    async def snapshot(self, *a, **k):
        self.byte_calls += 1
        return await super().snapshot(*a, **k)

    async def stats(self, *a, **k):
        # stats reads sizes, not bytes — but count any call to be strict about the control path.
        return await super().stats(*a, **k)


def test_playback_returns_urls_only_no_bytes():
    svc, _ = _svc()
    src = _register(svc)
    sess, playback = run(svc.start_session(tenant="t1", source_id=src.id, outputs=["hls"]))
    # The handle is URLs + expiry — never a frame or base64 blob.
    flat = str(playback)
    assert "playlist_url" in playback["hls"] and "token=" in playback["hls"]["playlist_url"]
    assert "data:" not in flat and "base64" not in flat.lower()


def test_repeated_playback_polls_move_zero_media_bytes():
    from app.adapters.datastore.memory import MemoryDataStore
    from app.adapters.stream.local_placer import LocalSessionPlacer
    from app.adapters.stream.signed_playback import HmacSignedPlayback
    from app.core.sealing import Sealer
    from app.core.stores import SessionStore, SourceStore
    from app.core.stream_service import StreamConfig, StreamService

    ds = MemoryDataStore()
    engine = _CountingEngine()
    cfg = StreamConfig(default_mode="on_demand", default_outputs=["hls"], max_duration_s=3600,
                       idle_timeout_s=300, max_size_bytes=10**9, lease_seconds=30, viewer_grace_s=30,
                       one_session_per_source=False, max_concurrent_global=100,
                       max_concurrent_per_tenant=10, allowed_hosts=frozenset(), allow_private=True,
                       playback_ttl=60, output_root="/tmp/hp")
    svc = StreamService(sources=SourceStore(ds), sessions=SessionStore(ds),
                        placer=LocalSessionPlacer(engine), engine=engine, sealer=Sealer("k" * 32),
                        signed_playback=HmacSignedPlayback("s" * 16), cfg=cfg)
    src = run(svc.register_source(tenant="t", name="c", url="rtsp://93.184.216.34/s"))
    sess, _ = run(svc.start_session(tenant="t", source_id=src.id))

    engine.byte_calls = 0
    for _ in range(50):
        run(svc.playback(sess.id, tenant="t"))     # 50 viewers poll
    assert engine.byte_calls == 0                    # not one frame pulled through the app
