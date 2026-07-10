# Phase 20 — search-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** search service — index any entity, query it
> with relevance ranking, facets, highlighting, typo/prefix tolerance, cursor pagination,
> and **Arabic-aware** analysis, all **hard tenant-isolated** and cached. Self-contained by
> default (a builtin inverted index over `DataStore`, zero deps); an external engine
> (OpenSearch/Meilisearch) can back it via the `SearchIndex` port. The **query layer,
> analyzer, and ranking are always ours**, so behaviour is identical whichever backend.
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code. Search is cross-cutting (§2/§15), so almost everything here is "past the checklist"._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only. No v2 — one pass, full._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Hot path? → YES (`search`). Cache it and prove zero reads on a repeat.**
- [ ] Results cached by `(tenant, gen, normalized_query, filters, sort, cursor, size)`, TTL `SEARCH_CACHE_TTL_SECONDS`, invalidated by a per-tenant **generation** bumped on any index write.
- [ ] **Benchmark**: a repeated identical query does **0 datastore reads and 0 backend calls** after warm; an index write bumps the generation so the next query recomputes.
- [ ] `index(doc)` writes the doc + its postings + one generation bump — no full scan.

**(2) Every config knob has a consumer → guard test from the start.**

**(3) What production needs that the checklist omits → build it.**
- [ ] **Hard tenant isolation** — the tenant filter is applied **server-side from the caller's identity**, never from client input. A search can never return another tenant's document. *(The #1 search vuln.)*
- [ ] **Cursor pagination** — an opaque cursor, not `offset` (offset skips/dupes rows when the index changes between pages).
- [ ] **Query-cost limits** — max query length, max terms, **no leading wildcard**, no user regex; a too-broad query is refused, not run. *(Wildcard/ReDoS DoS.)*
- [ ] **BM25-ish relevance ranking** with per-field boosts; deterministic tie-break by id so paging is stable.
- [ ] **Analyzer** — tokenize, lowercase-fold, Unicode NFKC, stopwords, **Arabic normalization** (strip tashkeel/diacritics; fold أ/إ/آ→ا, ى→ي, ة→ه), optional prefix/typo (edit-distance ≤1) match.
- [ ] **Facets** (counts per field value) + **highlighting** (matched snippets).
- [ ] **Access-filtered** results respecting the caller (tenant always; optional owner/visibility).
- [ ] **Degraded, not 500** — a backend outage returns a clear `unavailable`, not a stack trace.

**(4) Correctness footguns.**
- [ ] **Cross-tenant leakage** → tenant filter server-enforced + a test that another tenant's docs are never returned.
- [ ] **Query injection** → a **structured query DSL only**; no raw backend query string is ever accepted or forwarded (no-eval, like authz conditions).
- [ ] **Offset drift** → opaque cursor encoding the sort position + a tie-break id.
- [ ] **Wildcard/regex DoS** → cost caps; leading wildcard rejected.
- [ ] **Stale cache after index update** → generation invalidation (tested).
- [ ] **Arabic folding** → `مُحَمَّد` matches `محمد`; `إبراهيم` matches `ابراهيم` (tested).

### Functional
- [ ] Index/update/delete a document; bulk index; search with query + filters + facets + sort + cursor; suggest/autocomplete (prefix); reindex; per-index stats.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; **hot-path benchmark + config-knob guard** pass; live boot proves each item. **One pass, no v2.**

---

## 1. Ports

| Port | Drivers | Role |
|------|---------|------|
| `SearchIndex` | `builtin` (inverted index over DataStore) · `opensearch` (lazy `[opensearch]`) · `meilisearch` (lazy) · `null` | store postings + fetch candidate docs |

Reuses `DataStore` (docs + postings), `Cache` (hot path), `EventBus` (optional index-on-event), `Telemetry`. The **analyzer, query planner, and BM25 ranking are ours** (`core/*`), so the builtin and external backends rank identically.

## 2. Steps

**STATUS: ✅ ALL DONE — 178 tests, gate PASS, live verified. The config-knob guard caught unwired external-engine + typo knobs mid-build → removed the external ones (ship with the adapter), wired typo into suggest.**

| Task | Step |
|---|---|
| 20.1 | Skeleton |
| 20.2 | Config (every knob consumed) + `.env.example` |
| 20.3 | `core/analyzer.py` (tokenize/fold/NFKC/stopwords/**Arabic**) — pure |
| 20.4 | `core/query.py` (structured DSL + cost limits) + `core/cursor.py` (opaque) + `core/ranking.py` (BM25) — pure |
| 20.5 | Ports + adapters (builtin inverted index; null; external stubs) + registry |
| 20.6 | Models + stores (Document, postings, per-index meta) |
| 20.7 | Core `SearchService` — index/delete/bulk, **search (tenant-isolated, cached, cursor, facets, highlight)**, suggest, reindex, stats |
| 20.8 | Routes + manifest (internal index/search; admin indexes/reindex/stats) |
| 20.9 | Tests — hot-path benchmark, config-knob guard, tenant-isolation, injection, Arabic folding, cursor stability, cost limits, ranking, facets |
| 20.10 | Verify + live boot + docs + memory |

## 3. Out of scope
Vector/semantic search (an `embedding` port could back it later) · cross-index joins · learned ranking. The builtin engine ships first; external engines are drop-in via the port.
