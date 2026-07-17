"""StreamService core — register/probe, start/stop, modes, guards, quota, reconcile."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.core.models import ERROR, LIVE, STOPPED
from app.core.sealing import Sealer
from app.core.ssrf import SsrfBlocked
from app.core.stores import SessionStore, SourceStore
from app.core.stream_service import (
    QuotaExceeded, SourceNotFound, StreamConfig, StreamService,
)


class _Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _svc(clock=None, **over):
    ds = MemoryDataStore()
    engine = NullStreamEngine()
    cfg_kw = dict(default_mode="on_demand", default_outputs=["hls"], max_duration_s=3600,
                  idle_timeout_s=300, max_size_bytes=1_000_000, lease_seconds=30, viewer_grace_s=30,
                  one_session_per_source=False, max_concurrent_global=100,
                  max_concurrent_per_tenant=3, allowed_hosts=frozenset(), allow_private=True,
                  playback_ttl=60, output_root="/tmp/stream-test")
    cfg_kw.update(over)
    svc = StreamService(
        sources=SourceStore(ds), sessions=SessionStore(ds),
        placer=LocalSessionPlacer(engine), engine=engine,
        sealer=Sealer("k" * 32), signed_playback=HmacSignedPlayback("s" * 16),
        cfg=StreamConfig(**cfg_kw), clock=clock or _Clock())
    return svc, ds


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _register(svc, url="rtsp://93.184.216.34:554/s", **kw):
    return run(svc.register_source(tenant="t1", name="Cam", url=url, **kw))


def test_register_seals_and_redacts():
    svc, _ = _svc()
    src = _register(svc)
    assert src.url_host == "rtsp://93.184.216.34:554"
    assert "93.184.216.34" not in src.url_sealed          # sealed blob hides the host/creds


def test_register_rejects_ssrf():
    svc, _ = _svc(allow_private=False)
    with pytest.raises(SsrfBlocked):
        run(svc.register_source(tenant="t1", name="x", url="http://169.254.169.254/meta"))


def test_start_and_stop_session():
    svc, _ = _svc()
    src = _register(svc)
    sess, playback = run(svc.start_session(tenant="t1", source_id=src.id))
    assert sess.status == LIVE
    assert "hls" in playback and "token=" in playback["hls"]["playlist_url"]
    out = run(svc.stop_session(sess.id, tenant="t1"))
    assert out.status == STOPPED and out.reason == "user"


def test_cross_tenant_source_and_session_hidden():
    svc, _ = _svc()
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    with pytest.raises(SourceNotFound):
        run(svc.get_source(src.id, tenant="other"))
    with pytest.raises(SourceNotFound):
        run(svc.get_session(sess.id, tenant="other"))


def test_concurrency_quota():
    svc, _ = _svc(max_concurrent_per_tenant=2)
    src = _register(svc)
    run(svc.start_session(tenant="t1", source_id=src.id))
    run(svc.start_session(tenant="t1", source_id=src.id))
    with pytest.raises(QuotaExceeded) as ei:
        run(svc.start_session(tenant="t1", source_id=src.id))
    assert ei.value.dimension == "concurrency"


def test_guards_are_clamped_to_service_max():
    svc, _ = _svc(max_duration_s=3600)
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id,
                                    guards={"max_duration_s": 999_999}))
    assert sess.guards.max_duration_s == 3600      # a tenant cannot ask for an unbounded stream


def test_max_duration_guard_stops_via_sweeper():
    clock = _Clock()
    svc, _ = _svc(clock=clock, max_duration_s=100)
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    clock.advance(101)
    res = run(svc.enforce_and_reconcile())
    assert res["stopped"] == 1
    assert run(svc.get_session(sess.id, tenant="t1")).reason == "max_duration"


def test_idle_guard_stops_a_viewer_driven_session():
    clock = _Clock()
    svc, _ = _svc(clock=clock, idle_timeout_s=60)
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id, mode="viewer_driven"))
    clock.advance(61)                              # no viewer refreshed within idle_timeout
    res = run(svc.enforce_and_reconcile())
    assert res["stopped"] == 1
    assert run(svc.get_session(sess.id, tenant="t1")).reason == "idle"


def test_viewer_refresh_keeps_session_alive():
    clock = _Clock()
    # Long lease isolates the idle behaviour from the heartbeat/zombie guard (tested separately).
    svc, _ = _svc(clock=clock, idle_timeout_s=60, lease_seconds=1_000_000)
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id, mode="viewer_driven"))
    clock.advance(40)
    run(svc.playback(sess.id, tenant="t1"))        # a viewer polled → resets idle
    clock.advance(40)                              # 40 since refresh < 60
    res = run(svc.enforce_and_reconcile())
    assert res["stopped"] == 0


def test_size_guard_stops_on_heartbeat():
    svc, _ = _svc(max_size_bytes=500_000)
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    out = run(svc.heartbeat(sess.id, stats={"bytes_out": 600_000}))
    assert out.status == STOPPED and out.reason == "max_size"


def test_reconcile_marks_dead_a_session_whose_engine_vanished():
    svc, ds = _svc()
    src = _register(svc)
    sess, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    # Simulate the engine process dying out from under us: stop it directly on the placer's engine.
    run(svc._placer._engine.stop(  # type: ignore[attr-defined]
        __import__("app.ports.stream_types", fromlist=["EngineHandle"]).EngineHandle(
            id=sess.placement["handle_id"])))
    # It's still in the placer registry, so reconcile won't call it dead here; instead force the
    # placer to forget it to model a worker crash:
    svc._placer._by_session.clear()  # type: ignore[attr-defined]
    res = run(svc.enforce_and_reconcile())
    assert res["reaped_dead"] == 1
    assert run(svc.get_session(sess.id, tenant="t1")).status == ERROR
