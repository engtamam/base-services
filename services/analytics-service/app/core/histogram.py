"""Mergeable fixed-bucket histogram — for percentiles that compose.

Why this exists: **you cannot average percentiles.** The mean of 24 hourly p95 latencies is
not the daily p95 — it is not any percentile of anything. Storing `p95` per bucket makes
every wider time range wrong.

A histogram is mergeable (bucket-wise addition is a monoid), so hourly histograms sum into a
daily one and the quantile is computed **once, at the end**, from the merged distribution.

Buckets are log-spaced, so a fixed small number of buckets covers microseconds → hours with
bounded relative error — the standard trade for latency data.
"""
from __future__ import annotations

import math

DEFAULT_BUCKETS = 64
_GROWTH = 1.25          # each bucket is 25% wider than the last
_MIN = 1e-3             # values below this land in bucket 0


def new(buckets: int = DEFAULT_BUCKETS) -> dict:
    n = max(8, min(256, int(buckets)))
    return {"n": n, "b": [0] * n, "count": 0, "sum": 0.0,
            "min": None, "max": None}


def _index(value: float, n: int) -> int:
    """Bucket `i` (i>0) covers (_MIN·g^(i-1), _MIN·g^i]; bucket 0 is (-inf, _MIN]."""
    if value <= _MIN:
        return 0
    idx = math.ceil(math.log(value / _MIN, _GROWTH))
    return max(0, min(n - 1, idx))


def _bucket_upper(i: int) -> float:
    return _MIN * (_GROWTH ** i) if i > 0 else _MIN


def _bucket_lower(i: int) -> float:
    return _MIN * (_GROWTH ** (i - 1)) if i > 1 else (0.0 if i == 0 else _MIN)


def add(hist: dict, value: float) -> dict:
    v = float(value)
    n = int(hist["n"])
    hist["b"][_index(v, n)] += 1
    hist["count"] = int(hist["count"]) + 1
    hist["sum"] = float(hist["sum"]) + v
    hist["min"] = v if hist["min"] is None else min(float(hist["min"]), v)
    hist["max"] = v if hist["max"] is None else max(float(hist["max"]), v)
    return hist


def merge(a: dict, b: dict) -> dict:
    """Bucket-wise addition — the operation that makes percentiles composable."""
    if a is None:
        return b
    if b is None:
        return a
    if int(a["n"]) != int(b["n"]):
        raise ValueError("cannot merge histograms with different bucket counts")
    mins = [x for x in (a["min"], b["min"]) if x is not None]
    maxs = [x for x in (a["max"], b["max"]) if x is not None]
    return {"n": int(a["n"]), "b": [x + y for x, y in zip(a["b"], b["b"])],
            "count": int(a["count"]) + int(b["count"]),
            "sum": float(a["sum"]) + float(b["sum"]),
            "min": min(mins) if mins else None,
            "max": max(maxs) if maxs else None}


def merge_all(hists: list[dict]) -> dict | None:
    out: dict | None = None
    for h in hists:
        out = merge(out, h) if out is not None else {**h, "b": list(h["b"])}
    return out


def quantile(hist: dict, q: float) -> float:
    """The q-quantile (0<q<1) of the MERGED distribution, clamped to observed [min,max]."""
    if not hist or int(hist["count"]) == 0:
        return 0.0
    count = int(hist["count"])
    target = max(0.0, min(1.0, float(q))) * count
    obs_lo = float(hist["min"] if hist["min"] is not None else 0.0)
    obs_hi = float(hist["max"] if hist["max"] is not None else 0.0)
    seen = 0
    for i, c in enumerate(hist["b"]):
        if c == 0:
            continue
        if seen + c >= target:
            # Interpolate WITHIN the bucket. Returning the bucket's upper edge biases every
            # percentile high by up to (_GROWTH-1); at 1.25 that is a silent +25% on your p95.
            lower, upper = _bucket_lower(i), _bucket_upper(i)
            frac = (target - seen) / c
            est = lower + frac * (upper - lower)
            return max(obs_lo, min(obs_hi, est))    # never report outside what we observed
        seen += c
    return obs_hi


def mean(hist: dict) -> float:
    c = int(hist.get("count", 0))
    return (float(hist["sum"]) / c) if c else 0.0
