"""Query — the read hot path. Reads ROLLUPS ONLY; never scans the raw event table.

The two aggregations everyone gets wrong:
  • `unique` is computed by **union-ing HLL sketches** across the buckets in range, then
    estimating once. Summing per-bucket unique counts over-counts every returning user.
  • `percentile` is computed by **merging histograms** across the range, then taking the
    quantile once. Averaging per-bucket p95s is not a percentile of anything.

Funnels and retention DO read raw events (they are sequence questions, not aggregations) —
they are explicitly not the hot path and are bounded by the query's time range.
"""
from __future__ import annotations

import json
from typing import Any

from app.core import buckets as _buckets
from app.core import hll as _hll
from app.core import histogram as _hist
from app.core import query as _q
from app.core.models import rollup_key
from app.core.stores import EventStore, RollupStore


class QueryService:
    def __init__(self, *, rollups: RollupStore, events: EventStore, warehouse, clock,
                 config: dict[str, Any], cache=None) -> None:
        self._rollups = rollups
        self._events = events
        self._warehouse = warehouse
        self._clock = clock
        self._cfg = config
        self._cache = cache

    async def _gen(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"analyticsgen:{tenant}")) or "0"
        except Exception:
            return "0"

    async def _cache_get(self, key: str):
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._cfg["cache_ttl"])
        except Exception:
            pass

    # ── metric query ────────────────────────────────────────────────────────────
    async def query(self, *, tenant: str, metric: str, agg: str = "count",
                    granularity: str = "day", start_ts: float, end_ts: float,
                    dimensions: list[str] | None = None, filters: Any = None,
                    percentile: float | None = None, tz_offset_minutes: int | None = None) -> dict:
        _q.validate(metric=metric, agg=agg, granularity=granularity, start_ts=start_ts,
                    end_ts=end_ts, dimensions=dimensions, percentile=percentile,
                    max_range_days=self._cfg["max_range_days"], max_groups=self._cfg["max_groups"])
        norm_filters = _q.normalize_filters(filters)
        tz = self._cfg["tz_offset"] if tz_offset_minutes is None else int(tz_offset_minutes)

        gen = await self._gen(tenant)
        ckey = "aq:" + json.dumps({"g": gen, "t": tenant, "m": metric, "a": agg, "gr": granularity,
                                   "s": start_ts, "e": end_ts, "d": sorted(dimensions or []),
                                   "f": norm_filters, "p": percentile, "tz": tz}, sort_keys=True)
        cached = await self._cache_get(ckey)
        if cached is not None:
            return {**cached, "cached": True}

        bucket_keys = _buckets.range_keys(start_ts, end_ts, granularity)
        if dimensions:
            result = await self._grouped(tenant, metric, agg, granularity, bucket_keys,
                                         dimensions, norm_filters, percentile, tz)
        else:
            keys = [rollup_key(tenant, metric, granularity, b, {}) for b in bucket_keys]
            rollups = await self._rollups.get_many(keys)      # keyed reads — no scan
            result = {"series": self._series(rollups, agg, percentile, tz, granularity),
                      "total": self._aggregate([r for r in rollups], agg, percentile)}

        await self._cache_set(ckey, result)
        return {**result, "cached": False}

    async def _grouped(self, tenant, metric, agg, granularity, bucket_keys, dimensions,
                       filters, percentile, tz) -> dict:
        groups: dict[str, list] = {}
        for b in bucket_keys:
            prefix = f"{tenant}|{metric}|{granularity}|{b}|"
            for r in await self._rollups.scan_prefix(prefix):
                if not r.dims:
                    continue
                if not all(d in r.dims for d in dimensions):
                    continue
                if not self._matches(r.dims, filters):
                    continue
                gkey = ",".join(f"{d}={r.dims[d]}" for d in dimensions)
                groups.setdefault(gkey, []).append(r)
        _q.check_groups(len(groups), self._cfg["max_groups"])
        return {"groups": {g: self._aggregate(rs, agg, percentile) for g, rs in groups.items()},
                "group_count": len(groups)}

    @staticmethod
    def _matches(dims: dict, filters: dict) -> bool:
        for k, want in filters.items():
            have = dims.get(k)
            if isinstance(want, list):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True

    def _series(self, rollups, agg, percentile, tz, granularity) -> list[dict]:
        out = []
        for r in sorted(rollups, key=lambda x: x.bucket):
            out.append({"bucket": _buckets.render_label(r.bucket, granularity, tz),
                        "utc_bucket": r.bucket,
                        "value": self._aggregate([r], agg, percentile)})
        return out

    def _aggregate(self, rollups: list, agg: str, percentile: float | None) -> float:
        if not rollups:
            return 0.0
        if agg == "count":
            return float(sum(r.count for r in rollups))
        if agg == "sum":
            return float(sum(r.sum for r in rollups))
        if agg == "avg":
            total = sum(r.count for r in rollups)
            return (sum(r.sum for r in rollups) / total) if total else 0.0
        if agg == "unique":
            # UNION the sketches, estimate ONCE. Summing per-bucket uniques over-counts.
            sketches = [r.hll for r in rollups if r.hll]
            merged = _hll.merge_all(sketches)
            return float(_hll.estimate(merged)) if merged else 0.0
        if agg == "percentile":
            # MERGE the histograms, take the quantile ONCE. Averaging p95s is meaningless.
            hists = [r.hist for r in rollups if r.hist and r.hist.get("count")]
            merged = _hist.merge_all(hists)
            return float(_hist.quantile(merged, percentile or 0.95)) if merged else 0.0
        return 0.0

    # ── funnel (sequence question — reads raw events, bounded by range) ─────────
    def _bound_range(self, start: float, end: float) -> None:
        """Reject an unbounded/reversed window for the raw-event scans (funnel/retention). Without it
        these authenticated routes had no range cap at all and pulled up to the whole per-tenant event
        set into memory per request."""
        if end < start:
            raise _q.QueryError("END_BEFORE_START")
        if (end - start) > int(self._cfg["max_range_days"]) * 86400:
            raise _q.QueryError("RANGE_TOO_LARGE")

    async def funnel(self, *, tenant: str, steps: list[str], start_ts: float, end_ts: float,
                     window_seconds: int) -> dict:
        self._bound_range(start_ts, end_ts)
        rows, truncated = await self._events.for_tenant_window(tenant, start_ts, end_ts)
        by_subject: dict[str, list] = {}
        for e in rows:
            if e.get("subject"):
                by_subject.setdefault(e["subject"], []).append(e)

        counts = [0] * len(steps)
        for subject, evs in by_subject.items():
            evs.sort(key=lambda x: x["event_time"])
            idx = 0
            t0 = None
            for e in evs:
                if idx >= len(steps):
                    break
                if e["name"] == steps[idx]:
                    if idx == 0:
                        t0 = e["event_time"]
                    elif e["event_time"] - t0 > window_seconds:
                        break                    # fell out of the conversion window
                    counts[idx] += 1
                    idx += 1
        conv = [{"step": s, "count": counts[i],
                 "rate": (counts[i] / counts[0]) if counts[0] else 0.0}
                for i, s in enumerate(steps)]
        return {"steps": conv, "entered": counts[0],
                "converted": counts[-1] if counts else 0, "truncated": truncated}

    # ── retention cohort ────────────────────────────────────────────────────────
    async def retention(self, *, tenant: str, event_name: str, cohort_start: float,
                        cohort_end: float, periods: int, period_seconds: int) -> dict:
        hi = cohort_end + max(0, int(periods)) * float(period_seconds)
        self._bound_range(cohort_start, hi)
        window, truncated = await self._events.for_tenant_window(tenant, cohort_start, hi)
        rows = [e for e in window if e.get("name") == event_name and e.get("subject")]
        cohort = {e["subject"] for e in rows
                  if cohort_start <= float(e["event_time"]) <= cohort_end}
        out = []
        for p in range(1, periods + 1):
            lo = cohort_end + (p - 1) * period_seconds
            hi = cohort_end + p * period_seconds
            active = {e["subject"] for e in rows if lo <= float(e["event_time"]) < hi
                      and e["subject"] in cohort}
            out.append({"period": p, "retained": len(active),
                        "rate": (len(active) / len(cohort)) if cohort else 0.0})
        return {"cohort_size": len(cohort), "periods": out, "truncated": truncated}

    # ── export ──────────────────────────────────────────────────────────────────
    def export(self, rows: list[dict]) -> tuple[str, bytes] | None:
        return self._warehouse.export(rows)
