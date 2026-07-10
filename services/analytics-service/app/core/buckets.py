"""Time bucketing — always UTC. Timezones are applied at RENDER time only.

The footgun: bucketing in local time. Under DST a local day has 23 or 25 hours, so local
buckets double-count or drop an hour, and a tenant that changes timezone silently rewrites
history. So every bucket key is derived from **UTC** and is immutable; a report renders the
label in the tenant's offset at the edge.

Events are bucketed by **event_time** (when it happened), never ingest_time (when we heard
about it) — otherwise a delayed batch lands entirely in "now".
"""
from __future__ import annotations

import datetime as _dt

GRANULARITIES = ("minute", "hour", "day", "month")

# The granularities ingest actually MATERIALISES as rollups. A query may only ask for these:
# asking for "minute" would read rollup rows that were never written and silently return
# zeros — a wrong answer is worse than a refused one. Minute stays in GRANULARITIES because
# bucket_key/range_keys are general utilities.
ROLLUP_GRANULARITIES = ("hour", "day", "month")

_FORMATS = {"minute": "%Y-%m-%dT%H:%M", "hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d",
            "month": "%Y-%m"}


def bucket_key(event_time: float, granularity: str) -> str:
    """A stable, sortable UTC bucket key."""
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {GRANULARITIES}")
    dt = _dt.datetime.fromtimestamp(float(event_time), _dt.timezone.utc)
    return dt.strftime(_FORMATS[granularity])


def bucket_start(key: str, granularity: str) -> float:
    """Epoch seconds of a bucket's start (UTC)."""
    dt = _dt.datetime.strptime(key, _FORMATS[granularity]).replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def range_keys(start_ts: float, end_ts: float, granularity: str) -> list[str]:
    """Every bucket key touched by [start, end] — the query reads exactly these rollup rows."""
    if start_ts > end_ts:
        return []
    step = {"minute": 60, "hour": 3600, "day": 86400}.get(granularity)
    keys: list[str] = []
    if granularity == "month":
        dt = _dt.datetime.fromtimestamp(start_ts, _dt.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        end = _dt.datetime.fromtimestamp(end_ts, _dt.timezone.utc)
        while dt <= end:
            keys.append(dt.strftime(_FORMATS["month"]))
            dt = (dt.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        return keys
    t = bucket_start(bucket_key(start_ts, granularity), granularity)
    while t <= end_ts:
        keys.append(bucket_key(t, granularity))
        t += step
    return keys


def render_label(key: str, granularity: str, tz_offset_minutes: int = 0) -> str:
    """Shift a UTC bucket label into the tenant's offset for DISPLAY only.

    A fixed offset (KSA is UTC+3 year-round) is deliberate: it cannot produce the
    duplicate/missing hours that a DST-aware *bucketing* scheme would.
    """
    if not tz_offset_minutes:
        return key
    dt = _dt.datetime.strptime(key, _FORMATS[granularity]).replace(tzinfo=_dt.timezone.utc)
    shifted = dt + _dt.timedelta(minutes=int(tz_offset_minutes))
    return shifted.strftime(_FORMATS[granularity])


def is_late(event_time: float, now: float, max_lateness_seconds: int) -> bool:
    return (now - float(event_time)) > max_lateness_seconds
