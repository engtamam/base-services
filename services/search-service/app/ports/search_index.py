"""SearchIndex port — persistence of documents + the inverted index.

The engine only STORES and RETRIEVES; the analyzer, BM25 ranking, tenant filtering, facets,
highlighting and pagination are all in the core service, so every backend ranks identically.
`postings(term)` returns the docs containing a term (+ their weighted term frequency) — the
query reads posting lists for its terms only, never scanning the whole corpus.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class IndexUnavailable(PortError):
    """The backend is unreachable — search returns 'degraded', never a 500."""


@runtime_checkable
class SearchIndex(Protocol):
    driver: str

    async def upsert(self, *, index: str, doc_id: str, record: dict[str, Any],
                     weighted_tf: dict[str, float]) -> None:
        ...

    async def delete(self, *, index: str, doc_id: str) -> None:
        ...

    async def get(self, *, index: str, doc_id: str) -> dict[str, Any] | None:
        ...

    async def postings(self, *, index: str, term: str) -> dict[str, float]:
        """doc_id -> weighted term frequency, for every doc containing `term`."""
        ...

    async def stats(self, *, index: str) -> dict[str, Any]:
        """{n_docs, total_len} — for BM25 idf + average document length."""
        ...

    async def iter_docs(self, *, index: str, limit: int) -> list[dict[str, Any]]:
        """Full document list (reindex / admin only — never on the query hot path)."""
        ...
