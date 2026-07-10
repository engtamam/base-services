"""Non-functional guards (per memory `phase-doc-rules`).

`search` is THE read-hot path. A repeated identical query must serve from cache with zero
index reads; an index write must bump the generation so the next query recomputes.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.index.builtin import BuiltinIndex
from app.core.search_service import SearchService

CFG = dict(arabic=True, stopwords=True, cache_ttl=300, default_size=20, max_size=100,
           max_query_len=512, max_query_terms=32, typo_tolerance=True, suggest_max=10, list_limit=100)


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.reads = self.writes = 0

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def scan_all(self, c, limit=100):
        self.reads += 1
        return await super().scan_all(c, limit)


def _svc(st):
    return SearchService(index=BuiltinIndex(st), cache=MemoryCache(), config=CFG)


def test_repeated_query_does_zero_index_reads_after_warm():
    st = CountingStore()
    svc = _svc(st)
    for i in range(20):
        run(svc.index(index="a", doc_id=f"d{i}", tenant="t1", fields={"t": "quick brown fox"}))
    run(svc.search(index="a", tenant="t1", text="fox"))       # warm the cache
    st.reads = 0
    for _ in range(50):
        run(svc.search(index="a", tenant="t1", text="fox"))
    assert st.reads == 0, f"cached search still did {st.reads} index reads"


def test_index_write_busts_the_query_cache():
    st = CountingStore()
    svc = _svc(st)
    run(svc.index(index="a", doc_id="d1", tenant="t1", fields={"t": "fox"}))
    assert run(svc.search(index="a", tenant="t1", text="fox"))["cached"] is False
    assert run(svc.search(index="a", tenant="t1", text="fox"))["cached"] is True
    run(svc.index(index="a", doc_id="d2", tenant="t1", fields={"t": "fox"}))
    assert run(svc.search(index="a", tenant="t1", text="fox"))["cached"] is False


def test_query_reads_only_term_postings_not_the_whole_corpus():
    st = CountingStore()
    svc = SearchService(index=BuiltinIndex(st), cache=None, config=CFG)   # no cache → measure raw reads
    for i in range(500):
        run(svc.index(index="a", doc_id=f"d{i}", tenant="t1", fields={"t": f"word{i % 10} common"}))
    st.reads = 0
    run(svc.search(index="a", tenant="t1", text="word3"))
    # reads ≈ postings(term) + stats + one get per candidate (the ~50 matching docs), NOT 500+
    assert st.reads < 120, f"query touched {st.reads} rows — looks like a full-corpus scan"


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
