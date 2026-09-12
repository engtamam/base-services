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
