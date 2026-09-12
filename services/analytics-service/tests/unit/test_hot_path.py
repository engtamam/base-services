"""Non-functional guards (per memory `phase-doc-rules`).

Analytics has TWO hot paths that need OPPOSITE treatments:

  • **Ingest is write-heavy.** Caching is the wrong lever; the guarantee is a bounded,
    scan-free write: exactly 1 event write + N rollup updates, and ZERO scans. A duplicate
    (our EventBus is at-least-once) must do ZERO additional writes.
  • **Query is read-heavy.** It is cached by tenant generation, must do ZERO datastore reads
    once warm, and must read ROLLUP rows only — never the raw event table. A query whose cost
    grows with the event count is a query that dies in production.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.ingest_service import IngestService
from app.core.query_service import QueryService
from app.core.stores import CardinalityStore, EventStore, RollupStore

BASE = 1_772_323_200.0          # 2026-03-01T00:00:00Z — fixed; no wall clock in tests


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    """Counts every datastore operation, per collection."""

    def __init__(self):
        super().__init__()
        self.reads = self.writes = self.scans = 0
        self.touched: list[str] = []

    async def get(self, c, k):
        self.reads += 1
        self.touched.append(c)
        return await super().get(c, k)

    async def cas(self, c, k, expected, v):
        ok = await super().cas(c, k, expected, v)
        if ok:
            self.writes += 1      # a successful cas is a write (ingest reserve + rollup updates use it)
        return ok

    async def put(self, c, k, v):
        self.writes += 1
        self.touched.append(c)
        return await super().put(c, k, v)

    async def scan_all(self, c, limit=100):
        self.scans += 1
        self.touched.append(c)
        return await super().scan_all(c, limit)


ICFG = {"pii_allowed": frozenset(), "max_cardinality": 1000, "late_max_seconds": 172_800,
        "hll_precision": 12, "hist_buckets": 128, "raw_retention_days": 90}
QCFG = {"cache_ttl": 60, "max_range_days": 400, "max_groups": 500, "tz_offset": 180}


def _env(now=BASE + 3600):
    st = CountingStore()
    clk = ManualClock(now)
    cache = MemoryCache()
    ing = IngestService(events=EventStore(st), rollups=RollupStore(st),
                        cardinality=CardinalityStore(st), clock=clk, config=ICFG, cache=cache)
    q = QueryService(rollups=RollupStore(st), events=EventStore(st), warehouse=None,
                     clock=clk, config=QCFG, cache=cache)
    return ing, q, st, clk


def _coro(ing, i, **kw):
    return ing.ingest(event_id=f"e{i}", tenant="t1", name="page.view",
                      event_time=kw.pop("event_time", BASE + 60),
                      subject=kw.pop("subject", f"u{i}"), value=kw.pop("value", 1.0), **kw)


def _ingest(ing, i, **kw):
    return run(_coro(ing, i, **kw))


def _ingest_many(ing, count: int, **kw) -> None:
    """Seed `count` events on ONE event loop.

    `asyncio.run()` builds and tears down a loop per call. Seeding 5 000 events one call at a
    time cost 62 seconds — 86% of this service's entire suite — none of it testing anything.
    The events, and every assertion about them, are unchanged.
    """
    async def _all():
        for i in range(count):
            await _coro(ing, i, **kw)

    run(_all())


# ── ingest: bounded, scan-free writes ───────────────────────────────────────
def test_ingest_writes_are_bounded_and_scan_free():
    ing, _, st, _ = _env()
    st.reads = st.writes = st.scans = 0
    _ingest(ing, 1)
    # 1 event + 1 seen-marker + 3 granularities x 1 total-rollup (no dims on this event).
    assert st.scans == 0, f"ingest performed {st.scans} scans — cost grows with table size"
    assert st.writes == 2 + 3, f"unexpected write amplification: {st.writes}"


def test_ingest_cost_does_not_grow_with_the_number_of_stored_events():
    ing, _, st, _ = _env()
    for i in range(200):
        _ingest(ing, i)
    st.reads = st.writes = st.scans = 0
    _ingest(ing, 999)
    assert st.scans == 0
    assert st.writes == 5, f"per-event writes grew to {st.writes} at 200 events"


def test_duplicate_event_does_zero_additional_writes():
    """At-least-once redelivery must not double-count. Wrong revenue is worse than no revenue."""
    ing, _, st, _ = _env()
    _ingest(ing, 1)
    st.reads = st.writes = st.scans = 0
    out = _ingest(ing, 1)
    assert out["duplicate"] is True
    assert st.writes == 0, f"duplicate performed {st.writes} writes — events get double-counted"
    assert st.scans == 0


def test_duplicate_does_not_change_the_counted_total():
    ing, q, _, _ = _env()
    for _ in range(5):
        _ingest(ing, 1)     # same event_id, five deliveries
    r = run(q.query(tenant="t1", metric="page.view", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 3600))
    assert r["total"] == 1.0


# ── query: cached, rollup-only reads ────────────────────────────────────────
def test_repeated_query_does_zero_datastore_reads_after_warm():
    ing, q, st, _ = _env()
    for i in range(50):
        _ingest(ing, i)
    kw = dict(tenant="t1", metric="page.view", agg="count", granularity="day",
              start_ts=BASE, end_ts=BASE + 3600)
    run(q.query(**kw))                       # warm
    st.reads = st.writes = st.scans = 0
    for _ in range(100):
        r = run(q.query(**kw))
    assert r["cached"] is True
    assert st.reads == 0, f"warm query did {st.reads} datastore reads"
    assert st.writes == 0, f"warm query did {st.writes} datastore WRITES"
    assert st.scans == 0


def test_a_write_invalidates_the_query_cache_immediately():
    """Generation-keyed cache: no stale analytics after an ingest."""
    ing, q, _, _ = _env()
    _ingest(ing, 1)
    kw = dict(tenant="t1", metric="page.view", agg="count", granularity="day",
              start_ts=BASE, end_ts=BASE + 3600)
    assert run(q.query(**kw))["total"] == 1.0
    _ingest(ing, 2)
    r = run(q.query(**kw))
    assert r["cached"] is False and r["total"] == 2.0


def test_query_never_reads_the_raw_event_table():
    """A metric query is served from the rollups. The raw events are never opened.

    Seeded with 1 000 events, not 5 000. Every assertion below is on OPERATIONS — which
    collections were touched, and that `scans` is exactly zero — so a larger pile of events
    proves nothing extra and cost 50 seconds, most of this service's suite. Each ingest rewrites
    the ~13 KB HyperLogLog sketch for its bucket, and the in-memory adapter deep-copies it twice.
    That the query stays cheap as events GROW is the neighbouring test's job, by measurement.
    """
    ing, q, st, _ = _env()
    _ingest_many(ing, 1_000)
    st.touched.clear()
    st.reads = st.scans = 0
    run(q.query(tenant="t1", metric="page.view", agg="count", granularity="day",
                start_ts=BASE, end_ts=BASE + 3600))
    assert "events" not in st.touched, "metric query touched the RAW EVENT table"
    assert st.scans == 0, f"metric query scanned {st.scans} times over 1000 events"
    assert set(st.touched) <= {"rollups"}, f"unexpected collections read: {set(st.touched)}"


def test_grouped_query_scans_rollups_only_never_events():
    ing, q, st, _ = _env()
    for i in range(100):
        _ingest(ing, i, props={"plan": "pro"})
    st.touched.clear()
    r = run(q.query(tenant="t1", metric="page.view", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 3600, dimensions=["plan"]))
    assert r["groups"] == {"plan=pro": 100.0}
    assert "events" not in st.touched, "group-by touched the RAW EVENT table"


def test_query_cost_is_flat_in_the_number_of_events():
    """1 000 events must cost a query no more reads than 100 — that is the whole point of rollups."""
    def reads_for(n):
        ing, q, st, _ = _env()
        _ingest_many(ing, n)
        st.reads = st.scans = 0
        run(q.query(tenant="t1", metric="page.view", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 3600))
        return st.reads + st.scans

    assert reads_for(100) == reads_for(1_000), "query cost grows with event count"


def test_cache_outage_never_breaks_a_query():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("down")

    st = MemoryDataStore()
    clk = ManualClock(BASE + 3600)
    ing = IngestService(events=EventStore(st), rollups=RollupStore(st),
                        cardinality=CardinalityStore(st), clock=clk, config=ICFG,
                        cache=BrokenCache())
    q = QueryService(rollups=RollupStore(st), events=EventStore(st), warehouse=None, clock=clk,
                     config=QCFG, cache=BrokenCache())
    _ingest(ing, 1)
    assert run(q.query(tenant="t1", metric="page.view", agg="count", granularity="day",
                       start_ts=BASE, end_ts=BASE + 3600))["total"] == 1.0


# ── the config-knob guard ───────────────────────────────────────────────────
def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
