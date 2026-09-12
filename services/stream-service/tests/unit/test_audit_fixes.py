"""Regression tests for stream-service audit findings #01, #02, #03, #05, #06, #07."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.core.models import Recording
from app.core.sealing import Sealer
from app.core.ssrf import SsrfBlocked
from app.core.stores import RecordingStore, SessionStore, SourceStore
from app.core.stream_service import QuotaExceeded, StreamConfig, StreamService


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Clock:
    def __call__(self):
        return 1_000_000.0


def _svc(ds=None, **over):
    ds = ds or MemoryDataStore()
    engine = NullStreamEngine()
    cfg_kw = dict(default_mode="on_demand", default_outputs=["hls"], max_duration_s=3600,
                  idle_timeout_s=300, max_size_bytes=1_000_000, lease_seconds=30, viewer_grace_s=30,
                  one_session_per_source=False, max_concurrent_global=100,
                  max_concurrent_per_tenant=3, allowed_hosts=frozenset(), allow_private=True,
                  playback_ttl=60, output_root="/tmp/stream-test")
    cfg_kw.update(over)
    svc = StreamService(sources=SourceStore(ds), sessions=SessionStore(ds),
                        placer=LocalSessionPlacer(engine), engine=engine, sealer=Sealer("k" * 32),
                        signed_playback=HmacSignedPlayback("s" * 16),
                        cfg=StreamConfig(**cfg_kw), clock=_Clock())
    return svc, ds


# ── 01: the connect URL is pinned to the validated IP ─────────────────────────
def test_pin_url_rewrites_host_to_ip():
    assert StreamService._pin_url("rtsp://u:p@cam.example:554/s", "10.0.0.5") \
        == "rtsp://u:p@10.0.0.5:554/s"
    assert StreamService._pin_url("rtsp://cam/s", "2001:db8::1") == "rtsp://[2001:db8::1]/s"


# ── 02: the ONVIF host is SSRF-validated at register ──────────────────────────
def test_register_rejects_metadata_onvif_host():
    svc, _ = _svc()
    with pytest.raises(SsrfBlocked):
        run(svc.register_source(tenant="t1", name="cam", url="rtsp://10.0.0.5:554/s",
                                onvif={"host": "169.254.169.254", "port": 80}))


# ── 03: the global concurrency cap is enforced ────────────────────────────────
def test_global_concurrency_cap_enforced():
    svc, ds = _svc(max_concurrent_global=1, max_concurrent_per_tenant=100)
    a = run(svc.register_source(tenant="t1", name="a", url="rtsp://10.0.0.5:554/s"))
    b = run(svc.register_source(tenant="t2", name="b", url="rtsp://10.0.0.6:554/s"))
    run(svc.start_session(tenant="t1", source_id=a.id))
    with pytest.raises(QuotaExceeded):
        run(svc.start_session(tenant="t2", source_id=b.id))   # global cap 1 — different tenant blocked


# ── 05: only one shipper holds the lease at a time ────────────────────────────
def test_ship_lease_is_exclusive_and_reclaimable_after_ttl():
    rs = RecordingStore(MemoryDataStore())
    assert run(rs.acquire_ship_lease("sess", 1000.0, ttl=300)) is True
    assert run(rs.acquire_ship_lease("sess", 1100.0, ttl=300)) is False   # still held
    assert run(rs.acquire_ship_lease("sess", 2000.0, ttl=300)) is True    # stale → reclaimed
    run(rs.release_ship_lease("sess"))
    assert run(rs.acquire_ship_lease("sess", 2001.0, ttl=300)) is True    # released → free


# ── 06: retention sweep stops once past the cutoff ────────────────────────────
def test_iter_expired_stops_at_first_non_expired():
    ds = MemoryDataStore()
    rs = RecordingStore(ds)
    for i, started in enumerate(["2020-01-01", "2020-06-01", "2030-01-01", "2031-01-01"]):
        run(rs.create(Recording(id=f"r{i}", tenant="t1", source_id="s", session_id=f"r{i}",
                                started=started, trigger="continuous")))
    expired, cursor = run(rs.iter_expired("2025-01-01", limit=100))
    assert {r.id for r in expired} == {"r0", "r1"}          # only the truly-expired
    assert cursor is None                                    # stopped — did not page the whole table


# ── 04: concurrent starts cannot breach the per-tenant concurrency cap ─────────
class _YieldStore(MemoryDataStore):
    """Yields the loop inside get(), so concurrent start_session coroutines actually interleave and
    exercise the reservation race (a plain MemoryDataStore never awaits internally)."""

    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def test_concurrent_starts_do_not_exceed_the_per_tenant_cap():
    ds = _YieldStore()
    svc, _ = _svc(ds=ds, max_concurrent_per_tenant=2, max_concurrent_global=100)
    srcs = [run(svc.register_source(tenant="t1", name=f"c{i}", url=f"rtsp://10.0.0.{i}:554/s"))
            for i in range(5)]

    async def _race():
        return await asyncio.gather(
            *[svc.start_session(tenant="t1", source_id=s.id) for s in srcs],
            return_exceptions=True)

    results = run(_race())
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, QuotaExceeded)]
    assert len(ok) == 2 and len(refused) == 3               # cap held under concurrency (pre-fix: >2)
    live = run(svc._sessions.count_live_for_tenant("t1", 10))
    assert live == 2                                         # exactly cap live sessions committed


def test_a_stopped_session_frees_a_concurrency_slot():
    svc, _ = _svc(max_concurrent_per_tenant=1)
    a = run(svc.register_source(tenant="t1", name="a", url="rtsp://10.0.0.5:554/s"))
    b = run(svc.register_source(tenant="t1", name="b", url="rtsp://10.0.0.6:554/s"))
    s1, _ = run(svc.start_session(tenant="t1", source_id=a.id))
    with pytest.raises(QuotaExceeded):
        run(svc.start_session(tenant="t1", source_id=b.id))  # cap 1 — full
    run(svc.stop_session(s1.id, tenant="t1"))                # releases the slot
    s2, _ = run(svc.start_session(tenant="t1", source_id=b.id))
    assert s2.status == "live"                               # slot reused after release


def test_one_session_per_source_is_atomic():
    ds = _YieldStore()
    svc, _ = _svc(ds=ds, one_session_per_source=True, max_concurrent_per_tenant=100)
    src = run(svc.register_source(tenant="t1", name="cam", url="rtsp://10.0.0.5:554/s"))

    async def _race():
        return await asyncio.gather(
            svc.start_session(tenant="t1", source_id=src.id),
            svc.start_session(tenant="t1", source_id=src.id),
            return_exceptions=True)

    results = run(_race())
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 1                                      # one camera, one live session (pre-fix: 2)
