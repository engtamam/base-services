"""Round-2 fixes (stream-service). Maps to docs/audits/round2/stream-service.json."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.core.sealing import Sealer
from app.core.ssrf import SsrfBlocked
from app.core.stores import SessionStore, SourceStore
from app.core.stream_service import StreamConfig, StreamService


class _Clock:
    t = 1_000_000.0
    def __call__(self): return self.t


class _Interleave(MemoryDataStore):
    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _cfg(**over):
    kw = dict(default_mode="on_demand", default_outputs=["hls"], max_duration_s=3600,
              idle_timeout_s=300, max_size_bytes=1_000_000, lease_seconds=30, viewer_grace_s=30,
              one_session_per_source=False, max_concurrent_global=100, max_concurrent_per_tenant=10,
              allowed_hosts=frozenset(), allow_private=True, playback_ttl=60,
              output_root="/tmp/stream-test")
    kw.update(over)
    return StreamConfig(**kw)


def _svc(ds, cfg=None, engine=None):
    engine = engine or NullStreamEngine()
    return StreamService(sources=SourceStore(ds), sessions=SessionStore(ds),
                         placer=LocalSessionPlacer(engine), engine=engine,
                         sealer=Sealer("k" * 32), signed_playback=HmacSignedPlayback("s" * 16),
                         cfg=cfg or _cfg(), clock=_Clock())


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _live(ds, key):
    row = run(ds.get("stream_quota", key))
    return int(row["live"]) if row else 0


# r2-01 — two concurrent stops of the SAME session release the reservation exactly once.
def test_concurrent_stop_releases_quota_once():
    ds = _Interleave()
    svc = _svc(ds)
    src = run(svc.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    a, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    run(svc.start_session(tenant="t1", source_id=src.id))     # B, keeps a live session around
    assert _live(ds, "t::t1") == 2

    async def _race():
        await asyncio.gather(
            svc.stop_session(a.id, tenant="t1"),
            svc.stop_session(a.id, tenant="t1"),
            return_exceptions=True,
        )
    run(_race())
    assert _live(ds, "t::t1") == 1        # one release for A, not two — B's slot is intact


# r2-04 — teardown releases what was RESERVED at start, even if the cap config changed since.
def test_release_uses_reserved_not_current_config():
    ds = MemoryDataStore()
    started = _svc(ds, cfg=_cfg(max_concurrent_per_tenant=5))
    src = run(started.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    sess, _ = run(started.start_session(tenant="t1", source_id=src.id))
    assert _live(ds, "t::t1") == 1

    # operator disables the per-tenant cap; stop via a service that sees the NEW config
    toggled = _svc(ds, cfg=_cfg(max_concurrent_per_tenant=0))
    run(toggled.stop_session(sess.id, tenant="t1"))
    assert _live(ds, "t::t1") == 0        # released the tenant slot it actually took (not leaked)


# r2-05 — a session-row create failure rolls back the reservation (no leaked slot).
def test_reservation_rolled_back_when_create_fails():
    class _FailCreate(MemoryDataStore):
        async def put(self, collection, key, value):
            if collection == "sessions":
                raise RuntimeError("create failed")
            return await super().put(collection, key, value)

    ds = _FailCreate()
    svc = _svc(ds)
    src = run(svc.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    with pytest.raises(RuntimeError):
        run(svc.start_session(tenant="t1", source_id=src.id))
    assert _live(ds, "t::t1") == 0        # reservation freed despite the create failure


# r2-02 — ONVIF host is re-validated (SSRF) at connect time.
def test_pin_onvif_refuses_a_blocked_host():
    svc = _svc(MemoryDataStore(), cfg=_cfg(allow_private=False))
    with pytest.raises(SsrfBlocked):
        svc.pin_onvif({"host": "169.254.169.254", "port": 80})
    assert svc.pin_onvif({}) == {}        # nothing to pin
