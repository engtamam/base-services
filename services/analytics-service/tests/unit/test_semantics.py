"""Correctness footguns: bucketing, lateness, timezones, cardinality, PII, query cost.

Each test here corresponds to a way analytics silently reports the wrong number.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.warehouse.csv_export import CsvWarehouse
from app.adapters.warehouse.null import NullWarehouse
from app.core import buckets as B
from app.core import query as Q
from app.core.ingest_service import IngestService
from app.core.models import OTHER
from app.core.query_service import QueryService
from app.core.stores import CardinalityStore, EventStore, RollupStore

BASE = 1_772_323_200.0          # 2026-03-01T00:00:00Z


def run(c):
    return asyncio.run(c)


def _env(now=BASE + 3600, **over):
    icfg = {"pii_allowed": frozenset(), "max_cardinality": 1000, "late_max_seconds": 172_800,
            "hll_precision": 12, "hist_buckets": 128, "raw_retention_days": 90, **over}
    qcfg = {"cache_ttl": 60, "max_range_days": 400, "max_groups": 500, "tz_offset": 180}
    st, clk, cache = MemoryDataStore(), ManualClock(now), MemoryCache()
    ing = IngestService(events=EventStore(st), rollups=RollupStore(st),
                        cardinality=CardinalityStore(st), clock=clk, config=icfg, cache=cache)
    q = QueryService(rollups=RollupStore(st), events=EventStore(st), warehouse=CsvWarehouse(),
                     clock=clk, config=qcfg, cache=cache)
    return ing, q, clk


# ── bucketing by event_time, in UTC ─────────────────────────────────────────
def test_events_are_bucketed_by_event_time_not_ingest_time():
    """A delayed batch belongs to the hour it describes, not the hour we heard about it."""
    ing, q, _ = _env(now=BASE + 7200)
    run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))   # hour 00
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 7200))
    assert [s["utc_bucket"] for s in r["series"]] == ["2026-03-01T00"]


def test_bucket_keys_are_utc_and_labels_render_in_tenant_timezone():
    key = B.bucket_key(BASE + 60, "hour")
    assert key == "2026-03-01T00"                          # storage is always UTC
    assert B.render_label(key, "hour", 180) == "2026-03-01T03"   # KSA UTC+3, display only


def test_a_timezone_change_cannot_rewrite_history():
    """Re-rendering in another offset must not move the stored bucket."""
    key = B.bucket_key(BASE + 60, "day")
    assert B.render_label(key, "day", 0) == key
    assert B.bucket_key(BASE + 60, "day") == key           # unchanged by any rendering


def test_range_keys_cover_every_bucket_in_the_range():
    assert B.range_keys(BASE, BASE + 2 * 86400, "day") == [
        "2026-03-01", "2026-03-02", "2026-03-03"]
    assert len(B.range_keys(BASE, BASE + 3 * 3600, "hour")) == 4


def test_month_bucketing_crosses_year_boundaries():
    jan = dt.datetime(2026, 12, 15, tzinfo=dt.timezone.utc).timestamp()
    feb = dt.datetime(2027, 2, 3, tzinfo=dt.timezone.utc).timestamp()
    assert B.range_keys(jan, feb, "month") == ["2026-12", "2027-01", "2027-02"]


# ── lateness ────────────────────────────────────────────────────────────────
def test_a_late_event_is_counted_separately_not_merged_into_a_reported_bucket():
    ing, q, _ = _env(now=BASE + 10 * 86400, late_max_seconds=86_400)
    out = run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))
    assert out["late"] is True

    onto = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                       start_ts=BASE, end_ts=BASE + 86400))
    late = run(q.query(tenant="t1", metric="hit.late", agg="count", granularity="day",
                       start_ts=BASE, end_ts=BASE + 86400))
    assert onto["total"] == 0.0, "a late event silently rewrote an already-reported bucket"
    assert late["total"] == 1.0, "a late event was dropped instead of counted"


def test_an_event_inside_the_watermark_is_not_late():
    ing, _, _ = _env(now=BASE + 3600, late_max_seconds=86_400)
    assert run(ing.ingest(event_id="e1", tenant="t1", name="hit",
                          event_time=BASE + 60))["late"] is False


# ── cardinality guard ───────────────────────────────────────────────────────
def test_high_cardinality_dimension_spills_to_other_instead_of_exploding_storage():
    # max_combinations high so the per-DIMENSION cap is what spills here, not the composite cap
    # (in production MAX_DIMENSION_COMBINATIONS >> MAX_DIMENSION_CARDINALITY, so they never collide).
    ing, q, _ = _env(max_cardinality=3, max_combinations=100)
    for i in range(10):
        run(ing.ingest(event_id=f"e{i}", tenant="t1", name="hit", event_time=BASE + 60,
                       props={"url": f"/p{i}"}))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400, dimensions=["url"]))
    groups = r["groups"]
    assert len(groups) == 4, groups                       # 3 admitted + __other__
    assert f"url={OTHER}" in groups
    assert groups[f"url={OTHER}"] == 7.0
    assert sum(groups.values()) == 10.0                   # nothing lost


def test_the_cardinality_cap_is_per_dimension_not_global():
    ing, q, _ = _env(max_cardinality=2)
    for i in range(2):
        run(ing.ingest(event_id=f"e{i}", tenant="t1", name="hit", event_time=BASE + 60,
                       props={"plan": f"p{i}", "country": f"c{i}"}))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400, dimensions=["plan"]))
    assert OTHER not in "".join(r["groups"])


# ── PII ─────────────────────────────────────────────────────────────────────
def test_pii_suspect_properties_are_dropped_by_default():
    ing, _, _ = _env()
    out = run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60,
                         props={"email": "a@b.c", "iqama": "123", "plan": "pro"}))
    assert out["dimensions"] == {"plan": "pro"}, "analytics is where PII leaks"


def test_pii_property_can_be_explicitly_allowlisted():
    ing, _, _ = _env(pii_allowed=frozenset({"email"}))
    out = run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60,
                         props={"email": "a@b.c", "phone": "1"}))
    assert out["dimensions"] == {"email": "a@b.c"}       # phone still dropped


# ── query DSL: no eval, cost limits, fail closed ────────────────────────────
def test_query_dsl_rejects_unknown_aggregation_and_granularity():
    kw = dict(metric="m", start_ts=0, end_ts=1, dimensions=None, percentile=None,
              max_range_days=400, max_groups=500)
    with pytest.raises(Q.QueryError):
        Q.validate(agg="drop_table", granularity="day", **kw)
    with pytest.raises(Q.QueryError):
        Q.validate(agg="count", granularity="year", **kw)


def test_query_refuses_a_granularity_that_is_never_rolled_up():
    """`minute` is a valid bucket key but is not materialised — answering 0 would be a lie."""
    assert "minute" not in Q.GRANULARITIES
    with pytest.raises(Q.QueryError):
        Q.validate(metric="m", agg="count", granularity="minute", start_ts=0, end_ts=1,
                   dimensions=None, percentile=None, max_range_days=400, max_groups=500)


def test_query_enforces_cost_limits():
    kw = dict(metric="m", agg="count", granularity="day", dimensions=None, percentile=None,
              max_range_days=1, max_groups=2)
    with pytest.raises(Q.QueryError):
        Q.validate(start_ts=0, end_ts=10 * 86400, **kw)          # range too large
    with pytest.raises(Q.QueryError):
        Q.validate(start_ts=10, end_ts=0, **kw)                  # end before start
    with pytest.raises(Q.QueryError):
        Q.check_groups(3, 2)                                     # too many groups
    with pytest.raises(Q.QueryError):
        Q.validate(start_ts=0, end_ts=1, metric="m", agg="count", granularity="day",
                   dimensions=["a", "b", "c", "d"], percentile=None, max_range_days=400,
                   max_groups=2)


def test_percentile_requires_a_valid_p():
    for p in (None, 0.0, 1.0, 1.5, -1):
        with pytest.raises(Q.QueryError):
            Q.validate(metric="m", agg="percentile", granularity="day", start_ts=0, end_ts=1,
                       dimensions=None, percentile=p, max_range_days=400, max_groups=500)


def test_filters_accept_only_equality_and_membership():
    assert Q.normalize_filters({"plan": "pro", "c": ["sa", "ae"]}) == {"plan": "pro",
                                                                       "c": ["sa", "ae"]}
    with pytest.raises(Q.QueryError):
        Q.normalize_filters({"plan": {"$ne": "pro"}})     # no operators -> nothing to inject
    with pytest.raises(Q.QueryError):
        Q.normalize_filters("plan='pro' OR 1=1")


def test_filters_narrow_grouped_results():
    ing, q, _ = _env()
    for i, plan in enumerate(["pro", "pro", "free"]):
        run(ing.ingest(event_id=f"e{i}", tenant="t1", name="hit", event_time=BASE + 60,
                       props={"plan": plan}))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day", start_ts=BASE,
                    end_ts=BASE + 86400, dimensions=["plan"], filters={"plan": "pro"}))
    assert r["groups"] == {"plan=pro": 2.0}


# ── aggregations end-to-end ─────────────────────────────────────────────────
def test_avg_is_a_weighted_mean_across_buckets_not_a_mean_of_means():
    ing, q, _ = _env(now=BASE + 3 * 86400, late_max_seconds=10 * 86400)
    for i in range(9):                       # day 0: nine events of value 1
        run(ing.ingest(event_id=f"a{i}", tenant="t1", name="v", event_time=BASE + 60, value=1.0))
    run(ing.ingest(event_id="b0", tenant="t1", name="v", event_time=BASE + 86400 + 60,
                   value=11.0))              # day 1: one event of value 11
    r = run(q.query(tenant="t1", metric="v", agg="avg", granularity="day",
                    start_ts=BASE, end_ts=BASE + 2 * 86400))
    assert r["total"] == 2.0                 # (9*1 + 11) / 10, not (1 + 11)/2 = 6


def test_unique_over_a_range_unions_buckets():
    ing, q, _ = _env(now=BASE + 3 * 86400, late_max_seconds=10 * 86400)
    for d in range(2):
        for u in range(100):                 # the same 100 users on both days
            run(ing.ingest(event_id=f"e{d}_{u}", tenant="t1", name="dau",
                           event_time=BASE + d * 86400 + 60, subject=f"u{u}"))
    r = run(q.query(tenant="t1", metric="dau", agg="unique", granularity="day",
                    start_ts=BASE, end_ts=BASE + 2 * 86400))
    assert r["total"] == 100.0               # not 200


def test_empty_range_returns_zero_not_an_error():
    _, q, _ = _env()
    r = run(q.query(tenant="t1", metric="nope", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400))
    assert r["total"] == 0.0 and r["series"] == []


def test_tenants_are_isolated_in_the_rollup_keyspace():
    ing, q, _ = _env()
    run(ing.ingest(event_id="e1", tenant="t1", name="hit", event_time=BASE + 60))
    run(ing.ingest(event_id="e2", tenant="t2", name="hit", event_time=BASE + 60))
    r = run(q.query(tenant="t1", metric="hit", agg="count", granularity="day",
                    start_ts=BASE, end_ts=BASE + 86400))
    assert r["total"] == 1.0


# ── funnel / retention ──────────────────────────────────────────────────────
def test_funnel_counts_ordered_steps_within_the_window():
    ing, q, _ = _env(now=BASE + 86400)
    steps = ["view", "add_to_cart", "purchase"]
    for u in range(10):                                    # 10 view
        run(ing.ingest(event_id=f"v{u}", tenant="t1", name="view",
                       event_time=BASE + 60, subject=f"u{u}"))
    for u in range(6):                                     # 6 add_to_cart
        run(ing.ingest(event_id=f"c{u}", tenant="t1", name="add_to_cart",
                       event_time=BASE + 120, subject=f"u{u}"))
    for u in range(3):                                     # 3 purchase
        run(ing.ingest(event_id=f"p{u}", tenant="t1", name="purchase",
                       event_time=BASE + 180, subject=f"u{u}"))
    r = run(q.funnel(tenant="t1", steps=steps, start_ts=BASE, end_ts=BASE + 86400,
                     window_seconds=3600))
    assert [s["count"] for s in r["steps"]] == [10, 6, 3]
    assert r["steps"][-1]["rate"] == 0.3


def test_funnel_excludes_conversions_outside_the_window():
    ing, q, _ = _env(now=BASE + 10 * 86400, late_max_seconds=10 * 86400)
    run(ing.ingest(event_id="v", tenant="t1", name="view", event_time=BASE, subject="u"))
    run(ing.ingest(event_id="p", tenant="t1", name="buy", event_time=BASE + 7200, subject="u"))
    inside = run(q.funnel(tenant="t1", steps=["view", "buy"], start_ts=BASE,
                          end_ts=BASE + 86400, window_seconds=10_800))
    outside = run(q.funnel(tenant="t1", steps=["view", "buy"], start_ts=BASE,
                           end_ts=BASE + 86400, window_seconds=3_600))
    assert inside["converted"] == 1
    assert outside["converted"] == 0


def test_retention_measures_the_cohort_that_came_back():
    ing, q, _ = _env(now=BASE + 10 * 86400, late_max_seconds=10 * 86400)
    for u in range(10):                                    # cohort of 10 on day 0
        run(ing.ingest(event_id=f"d0_{u}", tenant="t1", name="active",
                       event_time=BASE + 60, subject=f"u{u}"))
    for u in range(4):                                     # 4 return in period 1
        run(ing.ingest(event_id=f"d1_{u}", tenant="t1", name="active",
                       event_time=BASE + 86400 + 120, subject=f"u{u}"))
    r = run(q.retention(tenant="t1", event_name="active", cohort_start=BASE,
                        cohort_end=BASE + 3600, periods=2, period_seconds=86_400))
    assert r["cohort_size"] == 10
    assert r["periods"][0]["retained"] == 4 and r["periods"][0]["rate"] == 0.4
    assert r["periods"][1]["retained"] == 0


# ── warehouse port ──────────────────────────────────────────────────────────
def test_null_warehouse_returns_none_rather_than_pretending_to_export():
    assert NullWarehouse().export([{"a": 1}]) is None


def test_csv_export_neutralises_formula_injection():
    """CWE-1236: a cell opening with = + - @ executes when the CSV is opened in a spreadsheet."""
    _, payload = CsvWarehouse().export([{"bucket": "=cmd|'/c calc'!A1", "value": "+1"}])
    text = payload.decode()
    assert "'=cmd" in text and "'+1" in text
    assert "\n=cmd" not in text


def test_csv_export_of_an_empty_result_is_safe():
    out = CsvWarehouse().export([])
    assert out is None or out[1] == b"" or out[1].strip() == b""


def test_filters_without_dimensions_are_refused():
    from app.core.query import QueryError
    ing, q, _ = _env()
    run(ing.ingest(event_id="e1", tenant="t1", name="orders", event_time=BASE + 60,
                   props={"status": "ok"}))
    with pytest.raises(QueryError):
        run(q.query(tenant="t1", metric="orders", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 3600, filters={"status": "failed"}))


def test_filters_with_a_dimension_are_applied():
    ing, q, _ = _env()
    run(ing.ingest(event_id="e1", tenant="t1", name="orders", event_time=BASE + 60,
                   props={"status": "ok"}))
    run(ing.ingest(event_id="e2", tenant="t1", name="orders", event_time=BASE + 60,
                   props={"status": "failed"}))
    r = run(q.query(tenant="t1", metric="orders", agg="count", granularity="hour",
                    start_ts=BASE, end_ts=BASE + 3600, dimensions=["status"],
                    filters={"status": "failed"}))
    assert r["group_count"] == 1
    assert sum(r["groups"].values()) == 1
