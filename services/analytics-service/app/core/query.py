"""Structured query DSL + cost limits.

A caller names a metric, an aggregation, dimensions and a time range. There is **no raw
query string** and no expression language — nothing to inject, nothing to eval (same stance
as the authz conditions, search DSL and connector field maps). Queries that would be
expensive are refused rather than run.
"""
from __future__ import annotations

from typing import Any

from app.core.buckets import ROLLUP_GRANULARITIES as GRANULARITIES

AGGREGATIONS = ("count", "sum", "avg", "unique", "percentile")


class QueryError(ValueError):
    pass


def validate(*, metric: str, agg: str, granularity: str, start_ts: float, end_ts: float,
             dimensions: list[str] | None, percentile: float | None,
             max_range_days: int, max_groups: int) -> None:
    if not metric or not isinstance(metric, str):
        raise QueryError("METRIC_REQUIRED")
    if agg not in AGGREGATIONS:
        raise QueryError(f"AGG_MUST_BE_ONE_OF:{','.join(AGGREGATIONS)}")
    if granularity not in GRANULARITIES:
        raise QueryError(f"GRANULARITY_MUST_BE_ONE_OF:{','.join(GRANULARITIES)}")
    if end_ts < start_ts:
        raise QueryError("END_BEFORE_START")
    if (end_ts - start_ts) > max_range_days * 86400:
        raise QueryError("RANGE_TOO_LARGE")
    if dimensions and len(dimensions) > 3:
        raise QueryError("TOO_MANY_DIMENSIONS")
    if agg == "percentile":
        if percentile is None or not (0.0 < float(percentile) < 1.0):
            raise QueryError("PERCENTILE_MUST_BE_BETWEEN_0_AND_1")


def normalize_filters(filters: Any) -> dict[str, Any]:
    """Field == value (or value ∈ list). No operators → no injection."""
    if not filters:
        return {}
    if not isinstance(filters, dict):
        raise QueryError("FILTERS_MUST_BE_OBJECT")
    out: dict[str, Any] = {}
    for k, v in filters.items():
        if not isinstance(k, str) or not k:
            raise QueryError("BAD_FILTER_FIELD")
        if isinstance(v, (list, tuple)):
            out[k] = list(v)
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            raise QueryError("BAD_FILTER_VALUE")
    return out


def check_groups(n: int, max_groups: int) -> None:
    if n > max_groups:
        raise QueryError("TOO_MANY_GROUPS")
