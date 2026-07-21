"""Index lifecycle and the two read paths."""
from __future__ import annotations

import asyncio

from app.adapters.knowledge_source.memory import MemoryKnowledgeSource
from app.adapters.model.extractive import ExtractiveModel
from app.core.kb_service import KbService

CFG = {"chunk_max_chars": 900, "top_n": 6, "min_score": 0.0, "max_context_chars": 6000,
       "bm25_k1": 1.2, "bm25_b": 0.75, "max_answer_tokens": 700}

DOC = {"billing.vat": {"title": "VAT number", "body": "Fifteen digits, shown on every invoice.",
                       "anchor": "settings.vat"},
       "admin.purge": {"title": "Purge", "body": "Deletes permanently.", "requires": "records.purge"}}


def run(c):
    return asyncio.run(c)


def _svc(doc=DOC, source=None):
    return KbService(source=source or MemoryKnowledgeSource(raw=doc),
                     model=ExtractiveModel(), cfg=CFG)


class _Counting(MemoryKnowledgeSource):
    """Counts loads so we can prove the index is not rebuilt when nothing changed."""
    def __init__(self, raw):
        super().__init__(raw=raw)
        self.builds = 0
    async def load(self):
        self.builds += 1
        return await super().load()


def test_an_unchanged_kb_is_indexed_once():
    src = _Counting(DOC)
    svc = _svc(source=src)
    a = run(svc.index())
    b = run(svc.index())
    assert a is b, "the index was rebuilt for an unchanged KB"


def test_a_changed_kb_rebuilds_the_index():
    svc = _svc()
    first = run(svc.index())
    svc._source = MemoryKnowledgeSource(raw={**DOC, "new.one": {"title": "N", "body": "B"}})
    assert run(svc.index()) is not first
    assert len(run(svc.index())) > len(first)


def test_concurrent_questions_do_not_each_rebuild():
    # The thundering herd lands exactly when the KB is largest and the rebuild most expensive.
    svc = _svc()
    async def race():
        return await asyncio.gather(*(svc.index() for _ in range(8)))
    got = run(race())
    assert all(i is got[0] for i in got)


def test_the_anchor_path_returns_the_entry_without_a_model():
    e = run(_svc().lookup(anchor="settings.vat", surface="web", caps=frozenset()))
    assert e is not None and e.id == "billing.vat"


def test_the_anchor_path_still_enforces_capability():
    doc = {"x": {"title": "T", "body": "B", "anchor": "a", "requires": "need.this"}}
    assert run(_svc(doc).lookup(anchor="a", surface="web", caps=frozenset())) is None


def test_ask_returns_a_grounded_answer_with_citations():
    answer, hits = run(_svc().ask(question="vat number", surface="web", caps=frozenset(),
                                  lang="en", not_found_text="nope"))
    assert not answer.abstained and answer.citations and hits


def test_ask_abstains_when_nothing_is_relevant():
    answer, hits = run(_svc().ask(question="unrelated nonsense xyzzy", surface="web",
                                  caps=frozenset(), lang="en", not_found_text="nope"))
    assert answer.abstained and answer.text == "nope" and hits == []


def test_ask_never_surfaces_help_the_caller_cannot_use():
    answer, _ = run(_svc().ask(question="purge", surface="admin", caps=frozenset(),
                               lang="en", not_found_text="nope"))
    assert answer.abstained, "admin help reached a caller without the capability"
