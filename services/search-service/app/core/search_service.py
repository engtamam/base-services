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
import logging
from typing import Any

from app.core import analyzer, cursor as _cursor, query as _query, ranking
from app.core.analyzer import edit_distance_le1
from app.core.models import Document
from app.ports.search_index import IndexUnavailable

_log = logging.getLogger("search.cache")


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
            # A DROPPED generation bump means writes stay invisible behind cached pages until TTL —
            # a stale-serving window that used to be entirely silent. Never fail the write on a cache
            # outage, but make the window OBSERVABLE (search-r2-09).
            _log.warning("search generation bump dropped for tenant=%s — cache unavailable; "
                         "results may be stale until cache_ttl", tenant)

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
        # Pass tenant so a peer cannot delete another tenant's document under a shared (index, doc_id)
        # — the engine no-ops when the stored record belongs to a different tenant.
        await self._index.delete(index=index, doc_id=doc_id, tenant=tenant)
        await self._bump(tenant)
        return {"deleted": True, "id": doc_id}

    async def bulk(self, tenant: str, docs: list[dict]) -> dict:
        # Per-document result, never an all-or-nothing 500: one bad doc (e.g. a CrossTenantConflict)
        # used to abort the loop AFTER earlier docs were already indexed — a partial index the caller
        # could not tell from a clean failure, and unsafe to blindly retry (search-r2-11). Now every
        # doc is attempted and failures are reported by id, so a retry of only the failed ids is safe.
        indexed = 0
        errors: list[dict[str, str]] = []
        for d in docs:
            doc_id = str(d.get("id", ""))
            try:
                await self.index(index=d["index"], doc_id=doc_id, tenant=tenant,
                                 fields=d.get("fields") or {}, owner=d.get("owner", ""),
                                 boosts=d.get("boosts"), created=d.get("created", 0.0))
                indexed += 1
            except Exception as exc:  # noqa: BLE001 — report the failure, do not abort the batch
                errors.append({"id": doc_id, "error": type(exc).__name__})
        return {"indexed": indexed, "failed": errors}

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
        # Bound the candidate set: a single very common term can have a posting list of millions, and
        # the loop below does one datastore get PER candidate. Without a cap a common query fans out to
        # millions of reads + a full in-memory rank (a DoS, esp. with the rate limiter now wired). We
        # take up to `max_candidates` and stop; ranking is best-effort over that bound for pathological
        # queries — a documented cost ceiling, not silent unbounded work.
        cap = int(self._cfg.get("max_candidates", 10_000))
        candidate_tf: dict[str, dict[str, float]] = {}
        doc_freq: dict[str, int] = {}
        for term in set(terms):
            postings = await self._index.postings(index=index, term=term)
            doc_freq[term] = len(postings)
            for doc_id, tf in postings.items():
                candidate_tf.setdefault(doc_id, {})[term] = tf
                # Check the cap INSIDE the inner loop: a single high-frequency term's posting list can
                # be huge, and the old outer-only check let ONE such term add every doc_id (then one
                # get per candidate below) before the cap was consulted — an unbounded fan-out DoS.
                if len(candidate_tf) >= cap:
                    break
            if len(candidate_tf) >= cap:
                break

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
        total = len(ordered)          # the FULL match count — computed BEFORE cursor filtering, so it
        #                               stays stable across pages (it previously shrank every page
        #                               because total was taken after dropping everything before the cursor).
        if pos is not None:
            ordered = [h for h in ordered if _cursor.after(h[0], h[1], pos)]
        page = ordered[:size]

        facet_counts = self._facets(facets, [docs_cache[i] for _, i in ordered]) if facets else {}
        out_hits = []
        for s, doc_id in page:
            rec = docs_cache[doc_id]
            hit = {"id": doc_id, "score": round(s, 4), "fields": rec.get("fields") or {}}
            if highlight and terms:
                hit["highlights"] = self._highlight(rec.get("fields") or {}, terms)
            out_hits.append(hit)
        # More pages iff the REMAINING (post-cursor) set has more than this page — not `total > size`,
        # which (now that total is the full count) would emit a cursor to an empty page on the last one.
        next_cursor = _cursor.encode(page[-1][0], page[-1][1]) if len(ordered) > size else ""
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
        # tenant-scoped candidate docs (O(this tenant's rows), not a global window that a hot other
        # tenant could starve — search-r2-05)
        docs = await self._index.iter_docs(index=index, tenant=tenant, limit=self._cfg["list_limit"])
        for rec in docs:
            if rec.get("tenant") != tenant:                # defence in depth: engine already scoped
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
