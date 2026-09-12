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
    _CAS_RETRIES = 8

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _dk(index: str, doc_id: str) -> str:
        return f"{index}:{doc_id}"

    @staticmethod
    def _pk(index: str, term: str) -> str:
        return f"{index}:{term}"

    @staticmethod
    def _docs_of(row: dict | None) -> dict[str, float]:
        """The {doc_id: tf} map of a posting row. Rows are stored as {"docs": {...}, "version": n};
        a legacy flat row ({doc_id: tf, ...}) is read by stripping the reserved `version` field."""
        if not row:
            return {}
        if "docs" in row:
            return dict(row["docs"])
        return {k: v for k, v in row.items() if k != "version"}

    async def _cas_update(self, collection: str, key: str, mutate, default: dict) -> None:
        """Atomic read-modify-write via the store CAS. `mutate(current)` returns the new value (minus
        the version, which the CAS assigns). Retries on a lost race; without this two concurrent
        indexers each read a posting/meta row and the second overwrote the first — a document silently
        dropped from a term (search-r2-03) or n_docs/total_len drift skewing BM25 (search-r2-04)."""
        for _ in range(self._CAS_RETRIES):
            row = await self._store.get(collection, key)
            expected = int(row.get("version", 0)) if row else 0
            new = mutate(row if row is not None else dict(default))
            if await self._store.cas(collection, key, expected, new):
                return
        raise RuntimeError(f"{collection}:{key} CAS exhausted under contention")

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
            await self._cas_update(
                _POST, self._pk(index, term),
                lambda row: {"docs": {k: v for k, v in self._docs_of(row).items() if k != doc_id}},
                {"docs": {}})
        dl = float(old.get("doc_len", 0))
        await self._cas_update(
            _META, index,
            lambda m: {**{k: v for k, v in m.items() if k != "version"}, "index": index,
                       "n_docs": max(0, int(m.get("n_docs", 0)) - 1),
                       "total_len": max(0.0, float(m.get("total_len", 0)) - dl)},
            {"n_docs": 0, "total_len": 0.0})
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
            await self._cas_update(
                _POST, self._pk(index, term),
                lambda row, tf=tf: {"docs": {**self._docs_of(row), doc_id: tf}},
                {"docs": {}})
        dl = float(record.get("doc_len", 0))
        await self._cas_update(
            _META, index,
            lambda m: {**{k: v for k, v in m.items() if k != "version"}, "index": index,
                       "n_docs": int(m.get("n_docs", 0)) + 1,
                       "total_len": float(m.get("total_len", 0)) + dl},
            {"n_docs": 0, "total_len": 0.0})

    async def delete(self, *, index: str, doc_id: str, tenant: str | None = None) -> None:
        # tenant, when given, guards against deleting another tenant's doc under a shared (index, id).
        await self._remove_existing(index, doc_id, tenant=tenant)

    async def get(self, *, index: str, doc_id: str) -> dict[str, Any] | None:
        return await self._store.get(_DOCS, self._dk(index, doc_id))

    async def postings(self, *, index: str, term: str) -> dict[str, float]:
        return self._docs_of(await self._store.get(_POST, self._pk(index, term)))

    async def stats(self, *, index: str) -> dict[str, Any]:
        return await self._store.get(_META, index) or {"n_docs": 0, "total_len": 0.0}

    async def iter_docs(self, *, index: str, tenant: str, limit: int) -> list[dict[str, Any]]:
        # Autocomplete corpus for ONE (index, tenant). Was a global `list(_DOCS)` window filtered by
        # index only, with the tenant filter applied AFTER — so a hot index shared across tenants
        # filled the scan window with another tenant's rows and starved a small tenant's suggestions
        # to empty (search-r2-05). A tenant-scoped equality find() is O(that tenant's rows) and cannot
        # be starved by another tenant's volume.
        rows, _ = await self._store.find(_DOCS, {"index": index, "tenant": tenant},
                                         limit=min(int(limit) if limit else _SUGGEST_SCAN,
                                                   _SUGGEST_SCAN),
                                         sort=("id", "asc"))
        return rows

    async def list_indexes(self, *, limit: int) -> list[dict[str, Any]]:
        # The meta collection holds one small row per index; enumerating it IS the catalogue read,
        # not a shortcut around an index. Bounded by `limit`.
        rows = await self._store.list(_META, limit)
        return sorted(({"index": r["index"], "n_docs": int(r.get("n_docs", 0))}
                       for r in rows if r.get("index")),
                      key=lambda r: r["index"])
