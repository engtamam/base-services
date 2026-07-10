"""The two aggregations everyone gets wrong, pinned against EXACT answers.

  • You cannot sum uniques.       sum(daily uniques) double-counts every returning user.
  • You cannot average percentiles. mean(hourly p95) is not a percentile of anything.

Both are fixed by storing MERGEABLE sketches per bucket and collapsing them once, at query
time. These tests assert the merged sketch tracks the exact answer AND that the naive method
is measurably wrong — if the naive method ever became right, the sketches would be pointless.
"""
from __future__ import annotations

import random
import statistics

from app.core import hll as H
from app.core import histogram as G


def _exact_quantile(vals, q):
    return sorted(vals)[int(q * len(vals)) - 1]


# ── HyperLogLog: unique counts that compose ─────────────────────────────────
def test_hll_is_exact_for_small_cardinalities():
    """Sparse mode is an exact set. Telling a small tenant it had 494 of its 500 daily
    actives is indistinguishable from a bug, so we do not estimate until we must."""
    s = H.new(12)
    for i in range(500):
        s = H.add(s, f"u{i}")
    assert H.is_sparse(s)
    assert H.estimate(s) == 500      # exact, not an estimate


def test_hll_promotes_to_dense_past_the_sparse_threshold_and_stays_accurate():
    p = 12
    s = H.new(p)
    for i in range(H._sparse_max(p) + 1):
        s = H.add(s, f"u{i}")
    assert not H.is_sparse(s), "sketch must promote once the exact set outgrows the registers"
    n = H._sparse_max(p) + 1
    assert abs(H.estimate(s) - n) / n < 0.05


def test_merging_a_sparse_and_a_dense_sketch_is_correct():
    p = 12
    dense, sparse = H.new(p), H.new(p)
    for i in range(H._sparse_max(p) + 1):        # forces promotion
        dense = H.add(dense, f"u{i}")
    for i in range(50):                          # a subset — the union must not grow
        sparse = H.add(sparse, f"u{i}")
    n = H._sparse_max(p) + 1
    merged = H.merge(dense, sparse)
    assert not H.is_sparse(merged)
    assert abs(H.estimate(merged) - n) / n < 0.05
    assert H.estimate(H.merge(sparse, dense)) == H.estimate(merged)   # order-independent


def test_two_sparse_sketches_whose_union_overflows_promote_on_merge():
    p = 12
    half = H._sparse_max(p) // 2 + 10
    a, b = H.new(p), H.new(p)
    for i in range(half):
        a = H.add(a, f"a{i}")
    for i in range(half):
        b = H.add(b, f"b{i}")
    assert H.is_sparse(a) and H.is_sparse(b)
    merged = H.merge(a, b)
    assert not H.is_sparse(merged)
    assert abs(H.estimate(merged) - 2 * half) / (2 * half) < 0.05


def test_sparse_union_of_overlapping_sketches_is_exactly_right():
    a, b = H.new(12), H.new(12)
    for i in range(300):
        a = H.add(a, f"u{i}")
    for i in range(200, 500):
        b = H.add(b, f"u{i}")
    assert H.estimate(H.merge(a, b)) == 500      # exact union, zero error


def test_hll_adding_the_same_value_twice_is_idempotent():
    a, b = H.new(12), H.new(12)
    for i in range(100):
        a = H.add(a, f"u{i}")
    for i in range(100):
        b = H.add(b, f"u{i}")
        b = H.add(b, f"u{i}")        # deliver it twice
    assert H.estimate(a) == H.estimate(b)


def test_hll_union_beats_summing_uniques_on_overlapping_days():
    """Day1 = users 0..999, Day2 = users 500..1499. Truth = 1500 uniques, not 2000."""
    d1, d2 = H.new(12), H.new(12)
    for i in range(0, 1000):
        d1 = H.add(d1, f"u{i}")
    for i in range(500, 1500):
        d2 = H.add(d2, f"u{i}")

    naive = H.estimate(d1) + H.estimate(d2)
    union = H.estimate(H.merge_all([d1, d2]))

    assert naive >= 1980, "the naive method should over-count — that is why HLL exists"
    assert abs(union - 1500) / 1500 < 0.05, f"HLL union {union} too far from 1500"
    assert abs(union - 1500) < abs(naive - 1500)


def test_hll_merge_is_commutative_and_associative():
    sketches = []
    for k in range(4):
        s = H.new(12)
        for i in range(k * 300, k * 300 + 500):     # deliberately overlapping
            s = H.add(s, f"u{i}")
        sketches.append(s)
    assert H.estimate(H.merge_all(sketches)) == H.estimate(H.merge_all(sketches[::-1]))


def test_hll_error_stays_within_tolerance_at_scale():
    s = H.new(12)
    for i in range(50_000):
        s = H.add(s, f"u{i}")
    assert abs(H.estimate(s) - 50_000) / 50_000 < 0.03


# ── Histogram: percentiles that compose ────────────────────────────────────
def test_merging_hourly_histograms_equals_one_histogram_over_all_events():
    random.seed(11)
    vals = [random.lognormvariate(3, 1) for _ in range(6_000)]
    hourly = []
    for h in range(24):
        g = G.new(128)
        for v in vals[h::24]:
            G.add(g, v)
        hourly.append(g)
    single = G.new(128)
    for v in vals:
        G.add(single, v)
    assert G.quantile(G.merge_all(hourly), 0.95) == G.quantile(single, 0.95)


def test_merged_percentile_tracks_exact_and_averaging_p95s_does_not():
    """Hours with different distributions: the mean of hourly p95s is simply wrong."""
    random.seed(12)
    fast = [random.uniform(1, 10) for _ in range(5_000)]     # a quiet hour
    slow = [random.uniform(100, 1_000) for _ in range(500)]  # an incident hour
    all_vals = fast + slow

    g_fast, g_slow = G.new(128), G.new(128)
    for v in fast:
        G.add(g_fast, v)
    for v in slow:
        G.add(g_slow, v)

    merged = G.quantile(G.merge_all([g_fast, g_slow]), 0.95)
    naive = (G.quantile(g_fast, 0.95) + G.quantile(g_slow, 0.95)) / 2
    exact = _exact_quantile(all_vals, 0.95)

    assert abs(merged - exact) / exact < 0.05, f"merged {merged} vs exact {exact}"
    assert abs(naive - exact) > abs(merged - exact), "averaging p95s should be worse"


def test_quantile_does_not_systematically_overestimate():
    """Returning the bucket's upper edge biases every percentile high; we interpolate."""
    random.seed(13)
    for q in (0.5, 0.95, 0.99):
        vals = [random.expovariate(1 / 50) for _ in range(20_000)]
        g = G.new(128)
        for v in vals:
            G.add(g, v)
        est, exact = G.quantile(g, q), _exact_quantile(vals, q)
        assert abs(est - exact) / exact < 0.05, f"q={q}: est={est} exact={exact}"


def test_quantile_is_clamped_to_observed_range():
    g = G.new(128)
    for v in (5.0, 5.0, 5.0):
        G.add(g, v)
    assert G.quantile(g, 0.99) == 5.0        # never invent a value we never saw


def test_median_of_a_single_bucket_is_sane_and_mean_is_exact():
    g = G.new(128)
    for v in (10.0, 20.0, 30.0, 40.0):
        G.add(g, v)
    assert G.mean(g) == 25.0                 # mean uses the exact running sum, not buckets
    assert 10.0 <= G.quantile(g, 0.5) <= 40.0


def test_histograms_with_different_bucket_counts_refuse_to_merge():
    import pytest
    with pytest.raises(ValueError):
        G.merge(G.new(64), G.new(128))


def test_empty_sketches_are_safe():
    assert G.quantile(G.new(128), 0.95) == 0.0
    assert H.estimate(H.new(12)) == 0
    assert G.merge_all([]) is None
    assert H.merge_all([]) is None
