"""Builtin inverted-index engine over the generic DataStore — self-contained, zero-dep.

  docs      : "<index>:<doc_id>"  -> the stored record (+ its term list, for clean deletes)
  postings  : "<index>:<term>"    -> {doc_id: weighted_tf}
  meta      : "<index>"           -> {n_docs, total_len}

A query reads only the posting lists of its terms — never the whole `docs` collection.
"""
from __future__ import annotations

from typing import Any

from app.ports.datastore import DataStore

_DOCS = "search_docs"
_POST = "search_postings"
_META = "search_meta"
# Upper bound on the corpus rows a single suggest() may scan (see iter_docs). Bounds the per-keystroke
# cost of the index-less prefix scan; was an unbounded 1,000,000.
_SUGGEST_SCAN = 10_000


class CrossTenantConflict(ValueError):
    """An upsert hit an existing (index, doc_id) owned by a DIFFERENT tenant. The storage key is
    (index, doc_id) with tenant only a field, so a shared index name + doc id would let one tenant
    silently overwrite another's document. We refuse loudly instead of corrupting."""


class BuiltinIndex:
    driver = "builtin"

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _dk(index: str, doc_id: str) -> str:
        return f"{index}:{doc_id}"

    @staticmethod
    def _pk(index: str, term: str) -> str:
        return f"{index}:{term}"

    async def _remove_existing(self, index: str, doc_id: str, *, tenant: str | None = None) -> None:
        old = await self._store.get(_DOCS, self._dk(index, doc_id))
        if not old:
            return
        # Never scrub a record that belongs to another tenant (delete path): the key omits tenant, so
        # a caller could otherwise remove another tenant's doc by guessing index+id. A None tenant
        # (internal re-index that already validated ownership) skips the check.
        if tenant is not None and old.get("tenant") != tenant:
            return
        for term in (old.get("_terms") or []):
            row = await self._store.get(_POST, self._pk(index, term)) or {}
            if doc_id in row:
                del row[doc_id]
                await self._store.put(_POST, self._pk(index, term), row)
        meta = await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}
        meta["n_docs"] = max(0, int(meta["n_docs"]) - 1)
        meta["total_len"] = max(0.0, float(meta["total_len"]) - float(old.get("doc_len", 0)))
        await self._store.put(_META, index, meta)
        await self._store.delete(_DOCS, self._dk(index, doc_id))

    async def upsert(self, *, index: str, doc_id: str, record: dict[str, Any],
                     weighted_tf: dict[str, float]) -> None:
        # Refuse to overwrite another tenant's document that happens to share this (index, doc_id).
        existing = await self._store.get(_DOCS, self._dk(index, doc_id))
        if existing and existing.get("tenant") != record.get("tenant"):
            raise CrossTenantConflict(
                f"{index}:{doc_id} is owned by another tenant — refusing cross-tenant overwrite")
        await self._remove_existing(index, doc_id, tenant=record.get("tenant"))   # idempotent re-index
        stored = {**record, "_terms": sorted(weighted_tf.keys())}
        await self._store.put(_DOCS, self._dk(index, doc_id), stored)
        for term, tf in weighted_tf.items():
            row = await self._store.get(_POST, self._pk(index, term)) or {}
            row[doc_id] = tf
            await self._store.put(_POST, self._pk(index, term), row)
        meta = await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}
        meta["index"] = index                          # carry the name so indexes can be enumerated
        meta["n_docs"] = int(meta["n_docs"]) + 1
        meta["total_len"] = float(meta["total_len"]) + float(record.get("doc_len", 0))
        await self._store.put(_META, index, meta)

    async def delete(self, *, index: str, doc_id: str, tenant: str | None = None) -> None:
        # tenant, when given, guards against deleting another tenant's doc under a shared (index, id).
        await self._remove_existing(index, doc_id, tenant=tenant)

    async def get(self, *, index: str, doc_id: str) -> dict[str, Any] | None:
        return await self._store.get(_DOCS, self._dk(index, doc_id))

    async def postings(self, *, index: str, term: str) -> dict[str, float]:
        return await self._store.get(_POST, self._pk(index, term)) or {}

    async def stats(self, *, index: str) -> dict[str, Any]:
        return await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}

    async def iter_docs(self, *, index: str, limit: int) -> list[dict[str, Any]]:
        # BOUNDED best-effort for autocomplete. This was `list(_DOCS, 1_000_000)` — a full scan of
        # EVERY index and tenant (up to a million rows) on every keystroke suggest, then filtered in
        # Python. There is no per-index doc index in the generic kv store, so suggest scans; bounding
        # the read at `_SUGGEST_SCAN` caps the cost. Suggest is a convenience, so best-effort coverage
        # of a hot index is the right trade — never a million-row read behind an autocomplete.
        rows = [r for r in await self._store.list(_DOCS, _SUGGEST_SCAN) if r.get("index") == index]
        return rows[:limit]

    async def list_indexes(self, *, limit: int) -> list[dict[str, Any]]:
        # The meta collection holds one small row per index; enumerating it IS the catalogue read,
        # not a shortcut around an index. Bounded by `limit`.
        rows = await self._store.list(_META, limit)
        return sorted(({"index": r["index"], "n_docs": int(r.get("n_docs", 0))}
                       for r in rows if r.get("index")),
                      key=lambda r: r["index"])
