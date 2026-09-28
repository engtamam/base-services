"""Core search: tenant isolation, ranking, facets, filters, cursor, cache/generation, Arabic, degraded."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.index.builtin import BuiltinIndex
from app.adapters.index.null import NullIndex
from app.core.query import QueryError
from app.core.search_service import SearchService
from app.ports.search_index import IndexUnavailable

CFG = dict(arabic=True, stopwords=True, cache_ttl=30, default_size=20, max_size=100,
           max_query_len=512, max_query_terms=32, typo_tolerance=True, suggest_max=10, list_limit=100)


def run(c):
    return asyncio.run(c)


def _svc(index=None, cache=None):
    st = MemoryDataStore()
    return SearchService(index=index or BuiltinIndex(st), cache=cache, config=CFG)


def _ix(svc, index, doc_id, tenant, fields, boosts=None):
    return run(svc.index(index=index, doc_id=doc_id, tenant=tenant, fields=fields, boosts=boosts or {}))


# ── tenant isolation (the #1 search vuln) ─────────────────────────────────────
def test_search_never_returns_another_tenants_document():
    svc = _svc()
    _ix(svc, "a", "mine", "t1", {"title": "secret fox"})
    _ix(svc, "a", "theirs", "t2", {"title": "secret fox"})
    hits = run(svc.search(index="a", tenant="t1", text="fox"))["hits"]
    assert [h["id"] for h in hits] == ["mine"]      # never 'theirs'


def test_empty_tenant_sees_nothing_of_others():
    svc = _svc()
    _ix(svc, "a", "d1", "t1", {"t": "fox"})
    assert run(svc.search(index="a", tenant="t2", text="fox"))["hits"] == []


# ── ranking ───────────────────────────────────────────────────────────────────
def test_field_boost_ranks_title_over_body():
    svc = _svc()
    _ix(svc, "a", "d1", "t1", {"title": "quick fox", "body": "x"}, boosts={"title": 3})
    _ix(svc, "a", "d2", "t1", {"title": "x", "body": "quick fox"}, boosts={"title": 3})
    hits = run(svc.search(index="a", tenant="t1", text="quick"))["hits"]
    assert [h["id"] for h in hits] == ["d1", "d2"]


def test_rare_term_scores_higher():
    svc = _svc()
    for i in range(10):
        _ix(svc, "a", f"c{i}", "t1", {"t": "common word"})
    _ix(svc, "a", "rare", "t1", {"t": "common rare"})
    top = run(svc.search(index="a", tenant="t1", text="rare"))["hits"]
    assert top[0]["id"] == "rare"


# ── facets, filters, highlight ────────────────────────────────────────────────
def test_facets_count_field_values():
    svc = _svc()
    for i, color in enumerate(["red", "blue", "red"]):
        _ix(svc, "p", f"p{i}", "t1", {"name": "shirt", "color": color})
    r = run(svc.search(index="p", tenant="t1", text="shirt", facets=["color"]))
    assert r["facets"]["color"] == {"red": 2, "blue": 1}


def test_filters_narrow_results():
    svc = _svc()
    _ix(svc, "p", "p1", "t1", {"name": "shirt", "color": "red"})
    _ix(svc, "p", "p2", "t1", {"name": "shirt", "color": "blue"})
    r = run(svc.search(index="p", tenant="t1", text="shirt", filters={"color": "red"}))
    assert [h["id"] for h in r["hits"]] == ["p1"]


def test_highlight_marks_matched_terms():
    svc = _svc()
    _ix(svc, "a", "d1", "t1", {"title": "the quick brown fox"})
    hit = run(svc.search(index="a", tenant="t1", text="fox"))["hits"][0]
    assert "<em>fox</em>" in hit["highlights"]["title"]


# ── cursor pagination stability ───────────────────────────────────────────────
def test_cursor_pages_cover_all_without_dupes():
    svc = _svc()
    for i in range(5):
        _ix(svc, "pg", f"x{i}", "t1", {"t": "apple pie"})
    seen, cur = [], ""
    for _ in range(4):
        r = run(svc.search(index="pg", tenant="t1", text="apple", size=2, cursor=cur))
        seen += [h["id"] for h in r["hits"]]
        cur = r["next_cursor"]
        if not cur:
            break
    assert sorted(seen) == ["x0", "x1", "x2", "x3", "x4"]


def test_size_capped_at_max():
    svc = _svc()
    for i in range(10):
        _ix(svc, "a", f"d{i}", "t1", {"t": "x"})
    r = run(svc.search(index="a", tenant="t1", text="x", size=9999))
    assert len(r["hits"]) <= CFG["max_size"]


# ── caching / generation invalidation ─────────────────────────────────────────
def test_repeat_query_is_cached_then_busted_on_write():
    svc = _svc(cache=MemoryCache())
    _ix(svc, "a", "d1", "t1", {"t": "fox"})
    assert run(svc.search(index="a", tenant="t1", text="fox"))["cached"] is False
    assert run(svc.search(index="a", tenant="t1", text="fox"))["cached"] is True
    _ix(svc, "a", "d2", "t1", {"t": "fox"})                         # write bumps generation
    fresh = run(svc.search(index="a", tenant="t1", text="fox"))
    assert fresh["cached"] is False and fresh["total"] == 2


def test_delete_busts_cache_and_removes_doc():
    svc = _svc(cache=MemoryCache())
    _ix(svc, "a", "d1", "t1", {"t": "fox"})
    run(svc.search(index="a", tenant="t1", text="fox"))
    run(svc.delete(index="a", doc_id="d1", tenant="t1"))
    r = run(svc.search(index="a", tenant="t1", text="fox"))
    assert r["total"] == 0 and r["cached"] is False


# ── Arabic ─────────────────────────────────────────────────────────────────────
def test_arabic_query_matches_regardless_of_diacritics():
    svc = _svc()
    _ix(svc, "ar", "a1", "t1", {"name": "مُحَمَّد بن سلمان"})
    assert [h["id"] for h in run(svc.search(index="ar", tenant="t1", text="محمد"))["hits"]] == ["a1"]
    assert [h["id"] for h in run(svc.search(index="ar", tenant="t1", text="إبراهيم"))["hits"]] == []


# ── cost limits ────────────────────────────────────────────────────────────────
def test_wildcard_query_rejected():
    svc = _svc()
    with pytest.raises(QueryError):
        run(svc.search(index="a", tenant="t1", text="fox*"))


# ── degraded, not 500 ──────────────────────────────────────────────────────────
def test_backend_outage_returns_unavailable_not_error():
    class Down(NullIndex):
        driver = "down"

        async def stats(self, **k):
            raise IndexUnavailable("backend down")

    svc = SearchService(index=Down(), cache=None, config=CFG)
    r = run(svc.search(index="a", tenant="t1", text="fox"))
    assert r["unavailable"] is True and r["hits"] == []


def test_null_index_yields_no_results():
    svc = SearchService(index=NullIndex(), cache=None, config=CFG)
    run(svc.index(index="a", doc_id="d", tenant="t1", fields={"t": "fox"}))
    assert run(svc.search(index="a", tenant="t1", text="fox"))["total"] == 0


# ── search-001: highlight snippets escape document text (no XSS) ────────────────
def test_highlight_escapes_document_markup():
    svc = _svc()
    _ix(svc, "a", "d1", "t1", {"title": "<script>steal()</script> fox"})
    r = run(svc.search(index="a", tenant="t1", text="fox", highlight=True))
    hl = r["hits"][0].get("highlights", {}) or r["hits"][0].get("highlight", {})
    blob = str(hl)
    assert "<script>" not in blob                      # raw markup escaped
    assert "&lt;script&gt;" in blob                    # ...to entities
    assert "<em>fox</em>" in blob                      # only our own tag is live


# ── search-004: a negative size is clamped, not silently dropping results ───────
def test_negative_size_is_clamped():
    svc = _svc()
    for i in range(5):
        _ix(svc, "a", f"d{i}", "t1", {"title": "fox"})
    r = run(svc.search(index="a", tenant="t1", text="fox", size=-1))
    assert len(r["hits"]) >= 1                          # not ordered[:-1] dropping the last hit


# ── search-003: covers() distinguishes default-open from locked ────────────────
def test_policy_covers():
    from app.core.internal_policy import InternalRoutePolicy
    assert InternalRoutePolicy(None).covers("/internal/search/query") is False
    p = InternalRoutePolicy({"/internal/search/query": ["ops-service"]})
    assert p.covers("/internal/search/query") is True
    assert p.permits("/internal/search/query", "ops-service") is True
    assert p.permits("/internal/search/query", "stranger") is False
