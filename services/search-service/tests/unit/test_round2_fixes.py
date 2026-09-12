"""Round-2 fixes (search-service). Maps to docs/audits/round2/search-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.index.builtin import BuiltinIndex
from app.core.erasure import SearchErasure
from app.core.search_service import SearchService

CFG = dict(arabic=True, stopwords=True, cache_ttl=30, default_size=20, max_size=100,
           max_query_len=512, max_query_terms=32, typo_tolerance=True, suggest_max=10, list_limit=100)


def run(c):
    return asyncio.run(c)


class _Interleave(MemoryDataStore):
    """Force interleaving: yield the loop on every get so two gathered coroutines both read the same
    version before either writes (the classic lost-update window)."""
    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


# r2-03 / r2-04 — concurrent indexing does not lose a posting or drift the meta counters.
def test_concurrent_indexing_keeps_all_postings_and_correct_counts():
    st = _Interleave()
    svc = SearchService(index=BuiltinIndex(st), cache=None, config=CFG)

    async def _go():
        await asyncio.gather(
            svc.index(index="a", doc_id="d1", tenant="t1", fields={"t": "shared fox"}),
            svc.index(index="a", doc_id="d2", tenant="t1", fields={"t": "shared owl"}),
        )
    run(_go())
    post = run(BuiltinIndex(st).postings(index="a", term="shared"))
    assert set(post) == {"d1", "d2"}                 # neither doc lost from the term (was a lost update)
    stats = run(BuiltinIndex(st).stats(index="a"))
    assert int(stats["n_docs"]) == 2                 # no counter drift


# r2-01 — a single high-frequency term cannot fan out past max_candidates.
def test_high_frequency_term_is_capped():
    st = MemoryDataStore()
    cfg = {**CFG, "max_candidates": 5}
    svc = SearchService(index=BuiltinIndex(st), cache=None, config=cfg)
    for i in range(40):
        run(svc.index(index="a", doc_id=f"d{i}", tenant="t1", fields={"t": "common"}))
    res = run(svc.search(index="a", tenant="t1", text="common", size=50))
    # ranking is best-effort over the capped candidate set — never all 40 candidate reads
    assert res["total"] <= 5


# r2-02 — erasure bumps the cache generation so cached pages stop serving the erased doc.
def test_erasure_invalidates_result_cache():
    st = MemoryDataStore()
    cache = MemoryCache()
    svc = SearchService(index=BuiltinIndex(st), cache=cache, config=CFG)
    run(svc.index(index="a", doc_id="d1", tenant="t1", fields={"t": "alice secret"}, owner="alice"))
    # prime the cache
    assert run(svc.search(index="a", tenant="t1", text="alice"))["total"] == 1

    eraser = SearchErasure(store=st, index=BuiltinIndex(st), pii_salt="s", cache=cache)
    res = run(eraser.erase("t1", "alice"))
    assert res["erased"] == ["search_docs:1"]
    # a fresh search no longer returns the erased doc (generation was bumped → cache miss → re-run)
    assert run(svc.search(index="a", tenant="t1", text="alice"))["total"] == 0


# r2-05 — suggest is tenant-scoped and not starved by another tenant's volume.
def test_suggest_is_tenant_scoped():
    st = MemoryDataStore()
    svc = SearchService(index=BuiltinIndex(st), cache=None, config=CFG)
    for i in range(50):
        run(svc.index(index="a", doc_id=f"big{i}", tenant="whale", fields={"t": "zebra"}))
    run(svc.index(index="a", doc_id="small1", tenant="minnow", fields={"t": "zephyr"}))
    sug = run(svc.suggest(index="a", tenant="minnow", prefix="ze"))["suggestions"]
    assert "zephyr" in sug and "zebra" not in sug     # sees only its own docs


# r2-11 — bulk reports per-doc failures without aborting the batch.
def test_bulk_reports_per_doc_failure_without_aborting():
    st = MemoryDataStore()
    svc = SearchService(index=BuiltinIndex(st), cache=None, config=CFG)
    run(svc.index(index="a", doc_id="clash", tenant="other", fields={"t": "x"}))  # owned by 'other'
    out = run(svc.bulk("t1", [
        {"index": "a", "id": "ok1", "fields": {"t": "one"}},
        {"index": "a", "id": "clash", "fields": {"t": "two"}},   # CrossTenantConflict
        {"index": "a", "id": "ok2", "fields": {"t": "three"}},
    ]))
    assert out["indexed"] == 2
    assert [e["id"] for e in out["failed"]] == ["clash"]
