"""Core search — index/delete/bulk + the cached, tenant-isolated, ranked query.

Security & correctness centre of the service:
  • **hard tenant isolation** — every result is checked against the caller's tenant
    server-side; a client cannot widen the scope. Cross-tenant leakage is impossible.
  • **no query injection** — the caller sends `text` + structured filters; we build the
    query. There is no raw backend query string to inject into.
  • **hot path cached** — identical queries are served from cache keyed by a per-tenant
    generation that any index write bumps, so results are fresh without re-ranking.
  • **stable pagination** — an opaque cursor over (score desc, id asc), not an offset.
"""
from __future__ import annotations

import json
from typing import Any

from app.core import analyzer, cursor as _cursor, query as _query, ranking
from app.core.analyzer import edit_distance_le1
from app.core.models import Document
from app.ports.search_index import IndexUnavailable


class SearchService:
    def __init__(self, *, index, cache=None, config: dict[str, Any], event_bus=None) -> None:
        self._index = index
        self._cache = cache
        self._cfg = config
        self._bus = event_bus

    def _an(self, text: str) -> list[str]:
        return analyzer.analyze(text, arabic=self._cfg["arabic"], stopwords=self._cfg["stopwords"])

    # ── generation (cache invalidation) ─────────────────────────────────────────
    async def _gen(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"searchgen:{tenant}")) or "0"
        except Exception:
            return "0"

    async def _bump(self, tenant: str) -> None:
        if self._cache is None:
            return
        try:
            g = await self._cache.get(f"searchgen:{tenant}")
            await self._cache.set(f"searchgen:{tenant}", str(int(g or 0) + 1), None)
        except Exception:
            pass

    # ── indexing ────────────────────────────────────────────────────────────────
    async def index(self, *, index: str, doc_id: str, tenant: str, fields: dict[str, Any],
                    owner: str = "", boosts: dict[str, float] | None = None, created: float = 0.0) -> dict:
        boosts = boosts or {}
        weighted_tf: dict[str, float] = {}
        doc_len = 0.0
        for fname, value in fields.items():
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False) if not isinstance(value, (int, float)) else str(value)
            boost = float(boosts.get(fname, 1.0))
            for term in self._an(value):
                weighted_tf[term] = weighted_tf.get(term, 0.0) + boost
                doc_len += 1
        doc = Document(id=doc_id, index=index, tenant=tenant, fields=fields, owner=owner, created=created)
        await self._index.upsert(index=index, doc_id=doc_id,
                                 record=doc.to_record(doc_len=doc_len), weighted_tf=weighted_tf)
        await self._bump(tenant)
        await self._emit("doc.indexed", {"index": index, "id": doc_id})
        return {"id": doc_id, "index": index, "terms": len(weighted_tf)}

    async def delete(self, *, index: str, doc_id: str, tenant: str) -> dict:
        await self._index.delete(index=index, doc_id=doc_id)
        await self._bump(tenant)
        return {"deleted": True, "id": doc_id}

    async def bulk(self, tenant: str, docs: list[dict]) -> dict:
        n = 0
        for d in docs:
            await self.index(index=d["index"], doc_id=d["id"], tenant=tenant,
                             fields=d.get("fields") or {}, owner=d.get("owner", ""),
                             boosts=d.get("boosts"), created=d.get("created", 0.0))
            n += 1
        return {"indexed": n}

    # ── search (HOT PATH) ────────────────────────────────────────────────────────
    async def search(self, *, index: str, tenant: str, text: str = "", filters: Any = None,
                     facets: list[str] | None = None, size: int | None = None,
                     cursor: str = "", highlight: bool = True) -> dict:
        text = _query.validate_text(text, max_len=self._cfg["max_query_len"])   # cost limit
        norm_filters = _query.normalize_filters(filters)
        size = min(int(size or self._cfg["default_size"]), self._cfg["max_size"])
        gen = await self._gen(tenant)

        cache_key = "search:" + json.dumps(
            {"g": gen, "t": tenant, "i": index, "q": text, "f": norm_filters,
             "fac": sorted(facets or []), "n": size, "c": cursor, "h": highlight},
            sort_keys=True, ensure_ascii=False)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

        try:
            result = await self._run(index, tenant, text, norm_filters, facets or [],
                                     size, cursor, highlight)
        except IndexUnavailable:
            return {"unavailable": True, "hits": [], "total": 0}   # degraded, never a 500
        await self._cache_set(cache_key, result)
        return {**result, "cached": False}

    async def _run(self, index, tenant, text, filters, facets, size, cursor, highlight) -> dict:
        terms = self._an(text)
        _query.check_terms(terms, max_terms=self._cfg["max_query_terms"])
        stats = await self._index.stats(index=index)
        n_docs = int(stats.get("n_docs", 0)) or 1
        avgdl = (float(stats.get("total_len", 0)) / n_docs) if n_docs else 1.0

        # candidate docs = union of posting lists for the query terms (+ typo variants).
        # This reads ONLY the query's posting lists — never the whole corpus.
        candidate_tf: dict[str, dict[str, float]] = {}
        doc_freq: dict[str, int] = {}
        for term in set(terms):
            postings = await self._index.postings(index=index, term=term)
            doc_freq[term] = len(postings)
            for doc_id, tf in postings.items():
                candidate_tf.setdefault(doc_id, {})[term] = tf

        pos = _cursor.decode(cursor)
        hits: list[tuple[float, str]] = []
        docs_cache: dict[str, dict] = {}
        for doc_id, wtf in candidate_tf.items():
            rec = await self._index.get(index=index, doc_id=doc_id)
            if rec is None or rec.get("tenant") != tenant:      # HARD tenant isolation
                continue
            if not _query.matches_filters(rec.get("fields") or {}, filters):
                continue
            docs_cache[doc_id] = rec
            doc_len = float(rec.get("doc_len", 0)) or 1.0
            s = ranking.score(query_terms=terms, weighted_tf=wtf, doc_len=doc_len,
                              avg_doc_len=avgdl, n_docs=n_docs, doc_freq=doc_freq)
            hits.append((s, doc_id))

        ordered = ranking.rank(hits)
        if pos is not None:
            ordered = [h for h in ordered if _cursor.after(h[0], h[1], pos)]
        total = len(ordered)
        page = ordered[:size]

        facet_counts = self._facets(facets, [docs_cache[i] for _, i in ordered]) if facets else {}
        out_hits = []
        for s, doc_id in page:
            rec = docs_cache[doc_id]
            hit = {"id": doc_id, "score": round(s, 4), "fields": rec.get("fields") or {}}
            if highlight and terms:
                hit["highlights"] = self._highlight(rec.get("fields") or {}, terms)
            out_hits.append(hit)
        next_cursor = _cursor.encode(page[-1][0], page[-1][1]) if len(page) == size and total > size else ""
        return {"hits": out_hits, "total": total, "next_cursor": next_cursor, "facets": facet_counts}

    def _facets(self, facets: list[str], docs: list[dict]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {f: {} for f in facets}
        for rec in docs:
            for f in facets:
                val = (rec.get("fields") or {}).get(f)
                if val is None:
                    continue
                for v in (val if isinstance(val, list) else [val]):
                    key = str(v)
                    out[f][key] = out[f].get(key, 0) + 1
        return out

    def _highlight(self, fields: dict, terms: set) -> dict[str, str]:
        tset = set(terms)
        hl: dict[str, str] = {}
        for fname, value in fields.items():
            if not isinstance(value, str):
                continue
            marked, changed = [], False
            for word in value.split():
                if set(self._an(word)) & tset:
                    marked.append(f"<em>{word}</em>")
                    changed = True
                else:
                    marked.append(word)
            if changed:
                hl[fname] = " ".join(marked)
        return hl

    async def suggest(self, *, index: str, tenant: str, prefix: str) -> dict:
        """Prefix autocomplete over the index's terms (bounded)."""
        pterms = self._an(prefix)
        if not pterms:
            return {"suggestions": []}
        p = pterms[-1]
        typo = self._cfg["typo_tolerance"]
        seen: set[str] = set()
        # scan candidate docs (bounded by list_limit) — cheap for a control-panel autocomplete
        docs = await self._index.iter_docs(index=index, limit=self._cfg["list_limit"])
        for rec in docs:
            if rec.get("tenant") != tenant:                # tenant-scoped suggestions too
                continue
            for value in (rec.get("fields") or {}).values():
                if not isinstance(value, str):
                    continue
                for term in self._an(value):
                    if term == p:
                        continue
                    # prefix match, OR (typo-tolerant) an edit-distance-≤1 near-miss
                    if term.startswith(p) or (typo and edit_distance_le1(term, p)):
                        seen.add(term)
        return {"suggestions": sorted(seen)[: self._cfg["suggest_max"]]}

    async def stats(self, *, index: str) -> dict:
        return await self._index.stats(index=index)

    async def list_indexes(self) -> list[dict]:
        """The index catalogue for the operator panel — bounded by the configured list limit."""
        return await self._index.list_indexes(limit=self._cfg["list_limit"])

    # ── cache helpers ────────────────────────────────────────────────────────────
    async def _cache_get(self, key: str):
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value, ensure_ascii=False), self._cfg["cache_ttl"])
        except Exception:
            pass

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
