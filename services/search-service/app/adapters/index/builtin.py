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
_SCAN = 1_000_000


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

    async def _remove_existing(self, index: str, doc_id: str) -> None:
        old = await self._store.get(_DOCS, self._dk(index, doc_id))
        if not old:
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
        await self._remove_existing(index, doc_id)         # idempotent re-index
        stored = {**record, "_terms": sorted(weighted_tf.keys())}
        await self._store.put(_DOCS, self._dk(index, doc_id), stored)
        for term, tf in weighted_tf.items():
            row = await self._store.get(_POST, self._pk(index, term)) or {}
            row[doc_id] = tf
            await self._store.put(_POST, self._pk(index, term), row)
        meta = await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}
        meta["n_docs"] = int(meta["n_docs"]) + 1
        meta["total_len"] = float(meta["total_len"]) + float(record.get("doc_len", 0))
        await self._store.put(_META, index, meta)

    async def delete(self, *, index: str, doc_id: str) -> None:
        await self._remove_existing(index, doc_id)

    async def get(self, *, index: str, doc_id: str) -> dict[str, Any] | None:
        return await self._store.get(_DOCS, self._dk(index, doc_id))

    async def postings(self, *, index: str, term: str) -> dict[str, float]:
        return await self._store.get(_POST, self._pk(index, term)) or {}

    async def stats(self, *, index: str) -> dict[str, Any]:
        return await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}

    async def iter_docs(self, *, index: str, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in await self._store.list(_DOCS, _SCAN) if r.get("index") == index]
        return rows[:limit]
