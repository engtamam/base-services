"""Round-2 adversarial audit fixes (analytics-service).

Each test corresponds to one confirmed finding in docs/audits/round2/analytics-service.json and
fails on the pre-fix code.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.warehouse.csv_export import CsvWarehouse
from app.core import hll as _hll
from app.core.ingest_service import IngestError, IngestService
from app.core.pii import is_subject_ref
from app.core.query_service import QueryService
from app.core.stores import CardinalityStore, EventStore, RollupStore, seen_key

BASE = 1_772_323_200.0          # 2026-03-01T00:00:00Z
SALT = "s" * 32


def run(c):
    return asyncio.run(c)


def _env(now=BASE + 3600, store=None, **over):
    icfg = {"pii_allowed": frozenset(), "max_cardinality": 1000, "max_combinations": 5000,
            "late_max_seconds": 172_800, "hll_precision": 12, "hist_buckets": 128,
            "raw_retention_days": 90, "pii_salt": SALT, "future_skew_seconds": 300, **over}
    qcfg = {"cache_ttl": 60, "max_range_days": 400, "max_groups": 500, "tz_offset": 180}
    st = store if store is not None else MemoryDataStore()
    clk, cache = ManualClock(now), MemoryCache()
    ing = IngestService(events=EventStore(st), rollups=RollupStore(st),
                        cardinality=CardinalityStore(st), clock=clk, config=icfg, cache=cache)
    q = QueryService(rollups=RollupStore(st), events=EventStore(st), warehouse=CsvWarehouse(),
                     clock=clk, config=qcfg, cache=cache)
    return ing, q, st


# r2-03 — idempotency is tenant-scoped: one tenant's event_id must not suppress another's.
def test_same_event_id_in_two_tenants_is_not_deduped():
    ing, q, _ = _env()
    a = run(ing.ingest(event_id="evt-123", tenant="t1", name="hit", event_time=BASE + 60))
    b = run(ing.ingest(event_id="evt-123", tenant="t2", name="hit", event_time=BASE + 60))
    assert a["duplicate"] is False
    assert b["duplicate"] is False           # was silently dropped as a cross-tenant duplicate
    for t in ("t1", "t2"):
        r = run(q.query(tenant=t, metric="hit", agg="count", granularity="day",
                        start_ts=BASE, end_ts=BASE + 86400))
        assert r["total"] == 1.0


# r2-04 — a failed ingest releases its reservation so at-least-once redelivery reprocesses.
def test_failed_ingest_releases_reservation_so_redelivery_is_reprocessed():
    class BreakOnce(MemoryDataStore):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def put(self, collection, key, value):
            if collection == "events" and self.fail:
                self.fail = False
                raise RuntimeError("boom mid-ingest")
            return await super().put(collection, key, value)

    st = BreakOnce()
    ing, q, _ = _env(store=st)
    with pytest.raises(RuntimeError):
        run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))
    # reservation was rolled back → the retry is NOT deduped and actually counts
    out = run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))
    assert out["duplicate"] is False
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400))
    assert r["total"] == 1.0


# r2-05 — rollup CAS exhaustion raises (never silently drops the increment).
def test_rollup_cas_exhaustion_raises_and_frees_the_reservation():
    class NeverWins(MemoryDataStore):
        async def cas(self, collection, key, expected_version, value):
            if collection == "rollups":
                return False                 # every rollup CAS loses the race
            return await super().cas(collection, key, expected_version, value)

    st = NeverWins()
    ing, _, _ = _env(store=st)
    with pytest.raises(RuntimeError):
        run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))
    # released, so a redelivery is reprocessed rather than silently deduped after a dropped count
    assert run(st.get("event_seen", seen_key("t1", "e1"))) is None


# r2-07 — non-finite value is rejected before it can poison a rollup.
@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_value_is_rejected(bad):
    ing, _, _ = _env()
    with pytest.raises(IngestError):
        run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60, value=bad))


# r2-09 — a far-future event_time is rejected (no unreachable, never-pruned future bucket).
def test_far_future_event_time_is_rejected():
    ing, _, _ = _env()
    with pytest.raises(IngestError):
        run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 10 * 86400))


# r2-06 / r2-13 — subject is pseudonymised before it is stored or hashed into the sketch.
def test_subject_is_pseudonymised_not_stored_raw():
    ing, _, st = _env()
    raw = "ahom59816@gmail.com"
    run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60, subject=raw))
    events = run(st.scan_all("events", 10))
    assert events and events[0]["subject"] != raw
    assert is_subject_ref(events[0]["subject"])         # a salted, tenant-scoped pseudonym
    # and the raw email's UNSALTED hash is not what the sketch stored
    rollups = run(st.scan_all("rollups", 10))
    raw_h = _hll._hash64(raw)
    for r in rollups:
        sk = r.get("hll") or {}
        assert raw_h not in (sk.get("s") or [])


# r2-08 — the composite dimension tuple is capped, not just each dimension independently.
def test_composite_dimension_combinations_are_capped():
    ing, q, _ = _env(max_cardinality=100, max_combinations=3)
    for i in range(10):
        run(ing.ingest(event_id=f"e{i}", tenant="t1", name="hit", event_time=BASE + 60,
                       props={"a": f"a{i}", "b": f"b{i}"}))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400, dimensions=["a", "b"]))
    assert len(r["groups"]) <= 3                         # bounded, not 10 distinct tuples
    # the total still counts every event (combinations spill to the no-dims total)
    tot = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                      start_ts=BASE, end_ts=BASE + 86400))
    assert tot["total"] == 10.0


# r2-15 — a malformed event mid-batch commits nothing (all-or-nothing on bad input).
def test_batch_with_a_bad_event_commits_nothing():
    ing, q, _ = _env()
    events = [{"id": f"e{i}", "name": "hit", "event_time": BASE + 60} for i in range(3)]
    events[2]["value"] = float("nan")                    # poison at index 2
    with pytest.raises(IngestError):
        run(ing.ingest_batch("t1", events))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400))
    assert r["total"] == 0.0                             # events 0,1 were NOT partially committed


# r2-01 / r2-02 — grouped query reads via an indexed range, and is correct across many buckets.
def test_grouped_query_is_correct_over_a_bucket_range():
    ing, q, _ = _env(now=BASE + 30 * 3600)
    for h in range(5):
        for _ in range(2):
            run(ing.ingest(event_id=f"e{h}-{_}", tenant="t1", name="hit",
                           event_time=BASE + h * 3600 + 60, props={"url": "/x"}))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 5 * 3600, dimensions=["url"]))
    assert r["groups"] == {"url=/x": 10.0}


# r2-12 — a non-grouped query batches its keyed reads (no per-bucket point read).
def test_non_grouped_query_uses_a_single_batch_read():
    class CountingStore(MemoryDataStore):
        def __init__(self):
            super().__init__()
            self.gets = 0
            self.batches = 0

        async def get(self, collection, key):
            if collection == "rollups":
                self.gets += 1
            return await super().get(collection, key)

        async def get_many(self, collection, keys):
            if collection == "rollups":
                self.batches += 1
            return await super().get_many(collection, keys)

    st = CountingStore()
    ing, q, _ = _env(now=BASE + 30 * 3600, store=st)
    for h in range(24):
        run(ing.ingest(event_id=f"e{h}", tenant="t1", name="hit", event_time=BASE + h * 3600 + 60))
    st.gets = 0
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 24 * 3600))
    assert r["total"] == 24.0
    assert st.batches == 1          # ONE batched read
    assert st.gets == 0             # not one point read per bucket
