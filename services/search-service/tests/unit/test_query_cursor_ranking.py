"""Query cost limits + injection resistance, cursor stability, BM25 ranking."""
from __future__ import annotations

import pytest

from app.core import cursor, ranking
from app.core.query import QueryError, check_terms, matches_filters, normalize_filters, validate_text


# ── query DSL / cost limits / no injection ────────────────────────────────────
def test_query_length_capped():
    validate_text("x" * 10, max_len=512)
    with pytest.raises(QueryError, match="TOO_LONG"):
        validate_text("x" * 600, max_len=512)


@pytest.mark.parametrize("bad", ["fox*", "fo?", "a\\b", "a/b", "a~2", "boost^3"])
def test_operator_chars_rejected(bad):
    # no wildcard / regex / lucene passthrough — the query is structured, never raw
    with pytest.raises(QueryError):
        validate_text(bad, max_len=512)


def test_plain_colon_is_allowed_because_there_is_no_passthrough():
    # a colon is just text (e.g. "12:30") — safe, because we never forward a raw query string
    assert validate_text("meeting at 12:30", max_len=512) == "meeting at 12:30"


def test_filters_are_structured_only():
    assert normalize_filters({"color": "red", "tags": ["a", "b"]}) == {"color": "red", "tags": ["a", "b"]}
    with pytest.raises(QueryError):
        normalize_filters({"x": {"$gt": 5}})      # no operator objects → no injection
    with pytest.raises(QueryError):
        normalize_filters([1, 2, 3])


def test_too_many_terms_rejected():
    check_terms(["a"] * 10, max_terms=32)
    with pytest.raises(QueryError, match="TOO_MANY"):
        check_terms(["a"] * 50, max_terms=32)


def test_matches_filters():
    fields = {"color": "red", "size": 10, "tags": ["x", "y"]}
    assert matches_filters(fields, {"color": "red"})
    assert not matches_filters(fields, {"color": "blue"})
    assert matches_filters(fields, {"color": ["red", "green"]})    # value ∈ list
    assert not matches_filters(fields, {"missing": "x"})


# ── cursor ────────────────────────────────────────────────────────────────────
def test_cursor_round_trips_exactly():
    enc = cursor.encode(0.8134729, "doc_9")
    assert cursor.decode(enc) == (0.8134729, "doc_9")     # exact, not rounded


def test_cursor_after_ordering():
    pos = (0.5, "m")
    assert cursor.after(0.4, "a", pos)          # lower score → after
    assert not cursor.after(0.6, "a", pos)      # higher score → before
    assert cursor.after(0.5, "z", pos)          # equal score, id after
    assert not cursor.after(0.5, "a", pos)      # equal score, id before


def test_malformed_cursor_is_ignored_not_crash():
    assert cursor.decode("!!!not-base64!!!") is None
    assert cursor.decode("") is None


# ── BM25 ──────────────────────────────────────────────────────────────────────
def test_idf_rewards_rare_terms():
    assert ranking.idf(1000, 1) > ranking.idf(1000, 500)    # rare > common


def test_score_zero_when_no_terms_match():
    s = ranking.score(query_terms=["x"], weighted_tf={}, doc_len=10, avg_doc_len=10,
                      n_docs=100, doc_freq={"x": 5})
    assert s == 0.0


def test_field_boost_raises_score():
    common = dict(query_terms=["fox"], doc_len=10, avg_doc_len=10, n_docs=100, doc_freq={"fox": 5})
    low = ranking.score(weighted_tf={"fox": 1.0}, **common)
    high = ranking.score(weighted_tf={"fox": 3.0}, **common)      # title boost
    assert high > low


def test_rank_is_a_total_order_score_then_id():
    ranked = ranking.rank([(0.5, "b"), (0.9, "a"), (0.5, "a")])
    assert ranked == [(0.9, "a"), (0.5, "a"), (0.5, "b")]
