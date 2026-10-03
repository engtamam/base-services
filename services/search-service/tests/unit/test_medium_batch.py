"""Non-security MEDIUM remediation (search-service):
  012  doc_freq is computed for EVERY query term even when the candidate cap is hit (correct IDF).
  013  the response carries total_is_estimate when the candidate cap bounds `total`; degraded shape matches.
  014  erasure stops (raises) instead of spinning forever when a matching row is not actually deleted.
  016  bool filters never match ints (True != 1).
  017  bulk reports per-doc failures classified retryable vs permanent, and rejects malformed docs up front.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.query import matches_filters


def run(c):
    return asyncio.run(c)


# ── 016: bool vs int filter equality ──────────────────────────────────────────
def test_bool_filter_does_not_match_int():
    assert matches_filters({"active": 1}, {"active": True}) is False
    assert matches_filters({"active": True}, {"active": True}) is True
    assert matches_filters({"active": 0}, {"active": False}) is False
    assert matches_filters({"n": 1}, {"n": 1}) is True           # plain ints still match
    assert matches_filters({"flag": True}, {"flag": [True, False]}) is True
    assert matches_filters({"flag": 1}, {"flag": [True, False]}) is False


# ── 014: erasure does not spin on an undeletable row ──────────────────────────
def test_erasure_raises_instead_of_spinning_when_delete_is_a_noop():
    from app.core.erasure import SearchErasure, _DOCS
    from app.adapters.datastore.memory import MemoryDataStore

    st = MemoryDataStore()
    run(st.put(_DOCS, "d1", {"id": "d1", "tenant": "t", "owner": "u", "index": "i", "created": 1.0}))

    class _Index:
        async def delete(self, *, index, doc_id, tenant):
            pass            # NO-OP: the row is never actually removed → old code spins forever

    er = SearchErasure(store=st, index=_Index(), pii_salt="s" * 16)
    with pytest.raises(RuntimeError, match="did not converge"):
        run(er.erase("t", "u"))


def test_erasure_deletes_and_acks_normally():
    from app.core.erasure import SearchErasure, _DOCS
    from app.adapters.datastore.memory import MemoryDataStore

    st = MemoryDataStore()
    run(st.put(_DOCS, "d1", {"id": "d1", "tenant": "t", "owner": "u", "index": "i", "created": 1.0}))

    class _Index:
        async def delete(self, *, index, doc_id, tenant):
            await st.delete(_DOCS, doc_id)

    out = run(SearchErasure(store=st, index=_Index(), pii_salt="s" * 16).erase("t", "u"))
    assert out["erased"] == ["search_docs:1"]


# ── 017: bulk failure classification ──────────────────────────────────────────
def _svc():
    from app.core.search_service import SearchService
    from app.adapters.datastore.memory import MemoryDataStore
    cfg = {"max_query_terms": 20, "max_query_len": 256, "max_candidates": 10_000,
           "typo_max_terms": 0, "suggest_max": 10, "list_limit": 100, "default_size": 10,
           "max_size": 100, "cache_ttl": 0}
    return SearchService(index=_RaisingIndex(), cache=None, config=cfg, event_bus=None)


class _RaisingIndex:
    driver = "fake"
    async def upsert(self, *, index, doc_id, record, weighted_tf):
        from app.ports.search_index import IndexUnavailable
        if index == "down":
            raise IndexUnavailable("backend down")
        raise ValueError("permanent boom")
    async def stats(self, **k): return {}
    async def postings(self, **k): return {}
    async def get(self, **k): return None


def test_bulk_malformed_doc_is_permanent_without_attempting():
    out = run(_svc().bulk("t", [{"fields": {}}]))        # no index/id
    assert out["indexed"] == 0
    err = out["failed"][0]
    assert err["error"] == "MalformedDoc" and err["retryable"] is False


def test_bulk_classifies_transient_vs_permanent():
    out = run(_svc().bulk("t", [
        {"id": "a", "index": "down", "fields": {}},     # IndexUnavailable → retryable
        {"id": "b", "index": "ok", "fields": {}},       # ValueError → permanent
    ]))
    by_id = {e["id"]: e for e in out["failed"]}
    assert by_id["a"]["retryable"] is True
    assert by_id["b"]["retryable"] is False
