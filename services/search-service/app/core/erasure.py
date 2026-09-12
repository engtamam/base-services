"""Erasure — search-service: what it deletes for a data subject, and what it keeps, with the reason.

The index is a DERIVED store, and derived stores are the leak everybody forgets: delete
the source row and the index still answers the person's name to anyone who searches for it.
So the subject's documents are removed from the index. Nothing is retained — an index can
always be rebuilt from a source that no longer holds the person.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


# The engine stores documents here (adapters/index/builtin.py `_DOCS`). The earlier value "documents"
# was a collection this service NEVER writes, so the erase found zero rows, scrubbed nothing, and still
# acked success — a silent DSAR no-op that left the person fully searchable (PDPL breach).
_DOCS = "search_docs"


class SearchErasure:
    service = "search-service"
    category = "search_index"

    def __init__(self, *, store, index, pii_salt: str, cache=None) -> None:
        self._store = store
        # The index engine: delete(index, doc_id, tenant) scrubs the doc row AND its inverted-index
        # postings AND the meta counters. Deleting only the raw row (the old bug) left the person's
        # terms in `search_postings`, still returned by search.
        self._index = index
        self._salt = pii_salt
        self._cache = cache

    async def _bump_generation(self, tenant: str) -> None:
        # Search results are cached per (tenant, generation). Deleting the docs is not enough: cached
        # result pages built before the erasure keep serving the person's data until cache_ttl expires
        # (search-r2-02). Bumping the tenant's generation invalidates every cached page at once —
        # the same mechanism SearchService uses after an index/delete.
        if self._cache is None:
            return
        try:
            g = await self._cache.get(f"searchgen:{tenant}")
            await self._cache.set(f"searchgen:{tenant}", str(int(g or 0) + 1), None)
        except Exception:
            pass

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = 0
        # Re-query from the start each pass: we delete what we read, so the next find returns the next
        # undeleted rows. A cursor would skip rows as the collection shrinks underneath it.
        while True:
            rows, _ = await self._store.find(_DOCS, {"tenant": tenant, "owner": subject},
                                             limit=500, sort=("created", "asc"))
            if not rows:
                break
            for r in rows:
                # tenant is passed so a cross-tenant record under a shared (index, id) is never touched.
                await self._index.delete(index=r["index"], doc_id=r["id"], tenant=tenant)
                deleted += 1
        if deleted:
            await self._bump_generation(tenant)

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"search_docs:{deleted}"] if deleted else [],
            "retained": [],   # a derived index is rebuildable; it may keep nothing
            "already_absent": deleted == 0,
        }
