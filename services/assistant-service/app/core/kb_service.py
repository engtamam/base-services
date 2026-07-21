"""The KB service — loads, indexes, retrieves, answers.

THE INDEX IS CACHED BY GENERATION, NOT BY TIME. A TTL rebuilds an unchanged KB on a timer (paying
for nothing) and serves a changed one until the timer expires (which is the half that matters — an
operator fixes a wrong answer, redeploys, and the assistant keeps giving the old one). Comparing
the source's content generation means a rebuild happens exactly when the content changed.

REBUILDS ARE SERIALISED. Without the lock, N concurrent questions arriving after a KB change all
discover the stale generation and all rebuild the index — the thundering herd lands precisely when
the KB is largest and the rebuild most expensive. One waits, the rest use the result.
"""
from __future__ import annotations

import asyncio

from app.core.answering import Answer, compose
from app.core.indexing import chunk_all
from app.core.kb import KbEntry
from app.core.retrieval import Hit, KbIndex, budgeted
from app.ports.knowledge_source import KnowledgeSource
from app.ports.model import Model


class KbService:
    def __init__(self, *, source: KnowledgeSource, model: Model, cfg: dict,
                 embeddings=None) -> None:
        self._source = source
        self._model = model
        self._cfg = cfg
        self._emb = embeddings
        self._index: KbIndex | None = None
        self._lock = asyncio.Lock()

    # ── index lifecycle ───────────────────────────────────────────────────────
    async def index(self) -> KbIndex:
        snap = await self._source.load()
        if self._index is not None and self._index.generation == snap.generation:
            return self._index
        async with self._lock:
            # Re-checked inside the lock: while waiting, another caller may have built exactly the
            # index we were about to build.
            if self._index is not None and self._index.generation == snap.generation:
                return self._index
            chunks = chunk_all(snap.entries, max_chars=self._cfg["chunk_max_chars"])
            index = KbIndex(snap.entries, chunks, generation=snap.generation,
                            k1=self._cfg["bm25_k1"], b=self._cfg["bm25_b"])
            await self._embed_index(index, chunks)
            self._index = index
            return self._index

    async def _embed_index(self, index: KbIndex, chunks) -> None:
        """Attach vectors if an embedder is configured. A failure here is NOT fatal.

        The lexical index is a complete product on its own, so an unreachable embedder degrades the
        ranking rather than the service. Turning an optional quality improvement into a hard
        dependency is how an assistant goes down because a similarity endpoint did.
        """
        if self._emb is None or not getattr(self._emb, "enabled", False) or not chunks:
            return
        try:
            vectors = await self._emb.embed([c.text for c in chunks])
            index.attach_vectors(vectors)
        except Exception:   # noqa: BLE001 — see the docstring: lexical still answers
            pass

    # ── the two read paths ────────────────────────────────────────────────────
    async def lookup(self, *, anchor: str, surface: str, caps: frozenset[str]) -> KbEntry | None:
        """The `?` icon: a dictionary lookup, no ranking and no model."""
        return (await self.index()).by_anchor(anchor, surface=surface, caps=caps)

    async def retrieve(self, *, question: str, surface: str, caps: frozenset[str],
                       lang: str) -> list[Hit]:
        idx = await self.index()
        # The QUERY vector is fetched per request and is equally optional: if it fails, the search
        # simply runs lexically, which is the same degradation as an unembedded index.
        qv = None
        if self._emb is not None and getattr(self._emb, "enabled", False):
            try:
                got = await self._emb.embed([question])
                qv = got[0] if got else None
            except Exception:   # noqa: BLE001
                qv = None
        hits = idx.search(question, surface=surface, caps=caps, lang=lang,
                          limit=self._cfg["top_n"], min_score=self._cfg["min_score"],
                          query_vector=qv, alpha=self._cfg.get("hybrid_alpha", 1.0))
        return budgeted(hits, max_chars=self._cfg["max_context_chars"])

    async def ask(self, *, question: str, surface: str, caps: frozenset[str], lang: str,
                  not_found_text: str, unavailable_text: str = "",
                  persona: str = "") -> tuple[Answer, list[Hit]]:
        """Retrieve, then answer or abstain. Hits are returned alongside so the caller can report
        what was searched even when nothing was good enough — an abstention that says only "I don't
        know" gives an operator nothing to act on."""
        hits = await self.retrieve(question=question, surface=surface, caps=caps, lang=lang)
        answer = await compose(question=question, hits=hits, model=self._model, lang=lang,
                               not_found_text=not_found_text,
                               unavailable_text=unavailable_text, persona=persona,
                               max_tokens=self._cfg["max_answer_tokens"])
        return answer, hits
