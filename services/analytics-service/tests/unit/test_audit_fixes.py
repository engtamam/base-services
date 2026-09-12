"""Regression tests for the 2026-09-12 fleet-audit findings on analytics-service."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.ingest_service import IngestService
from app.core.query import QueryError
from app.core.query_service import QueryService
from app.core.stores import CardinalityStore, EventStore, RollupStore

BASE = 1_772_323_200.0
ICFG = {"pii_allowed": frozenset(), "max_cardinality": 1000, "late_max_seconds": 172_800,
        "hll_precision": 12, "hist_buckets": 128, "raw_retention_days": 90}
QCFG = {"cache_ttl": 60, "max_range_days": 400, "max_groups": 500, "tz_offset": 180}


def run(c):
    return asyncio.run(c)


def _env():
    st = MemoryDataStore()
    clk = ManualClock(BASE + 3600)
    cache = MemoryCache()
    ing = IngestService(events=EventStore(st), rollups=RollupStore(st),
                        cardinality=CardinalityStore(st), clock=clk, config=ICFG, cache=cache)
    q = QueryService(rollups=RollupStore(st), events=EventStore(st), warehouse=None,
                     clock=clk, config=QCFG, cache=cache)
    return ing, q, st


def _count(q):
    r = run(q.query(tenant="t1", metric="m", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 7200, dimensions=[], filters={},
                    percentile=None, tz_offset_minutes=0))
    # sum the returned series/groups
    if r.get("series"):
        return sum(pt.get("value", 0) for pt in r["series"])
    return sum((r.get("groups") or {}).values()) or r.get("value", 0)


# ── 01: concurrent ingests into the same cell lose no increments ──────────────
def test_concurrent_ingests_do_not_undercount():
    ing, q, st = _env()

    async def _race():
        await asyncio.gather(*[
            ing.ingest(event_id=f"e{i}", tenant="t1", name="m", event_time=BASE + 60,
                       subject=f"u{i}", value=1.0)
            for i in range(12)])

    run(_race())
    assert _count(q) == 12          # every increment landed (CAS-retry); pre-fix lost racers


# ── 02: concurrent duplicate event_id counts once ─────────────────────────────
def test_concurrent_duplicate_event_counts_once():
    ing, q, st = _env()

    async def _race():
        return await asyncio.gather(
            ing.ingest(event_id="dup", tenant="t1", name="m", event_time=BASE + 60,
                       subject="u", value=1.0),
            ing.ingest(event_id="dup", tenant="t1", name="m", event_time=BASE + 60,
                       subject="u", value=1.0))

    res = run(_race())
    dups = [r["duplicate"] for r in res]
    assert dups.count(False) == 1 and dups.count(True) == 1
    assert _count(q) == 1           # atomic reserve prevented a double count


# ── 03: window read reports truncation ────────────────────────────────────────
def test_for_tenant_window_flags_truncation():
    st = MemoryDataStore()
    events = EventStore(st)
    for i in range(5):
        run(events.add(_ev(i)))

    rows, truncated = run(events.for_tenant_window("t1", BASE, BASE + 10_000, limit=3))
    assert len(rows) == 3 and truncated is True
    rows2, truncated2 = run(events.for_tenant_window("t1", BASE, BASE + 10_000, limit=100))
    assert len(rows2) == 5 and truncated2 is False


def _ev(i):
    from app.core.models import Event
    return Event(id=f"e{i}", tenant="t1", name="m", event_time=BASE + i, ingest_time=BASE + i,
                 subject=f"u{i}", value=1.0, props={})


# ── 04: funnel/retention reject an over-wide range ────────────────────────────
def test_funnel_rejects_a_range_over_the_cap():
    ing, q, st = _env()
    too_wide = BASE + (QCFG["max_range_days"] + 5) * 86400
    with pytest.raises(QueryError):
        run(q.funnel(tenant="t1", steps=["a", "b"], start_ts=BASE, end_ts=too_wide,
                     window_seconds=3600))
