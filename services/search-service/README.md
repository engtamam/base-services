# search-service

> Self-contained, **general & plug-and-play** full-text search — index any entity, query it
> with **BM25 relevance**, facets, highlighting, cursor pagination, and **Arabic-aware**
> analysis, all **hard tenant-isolated** and cached. Self-contained by default (a builtin
> inverted index over `DataStore`, zero deps); the **analyzer, query planner and ranking
> are always ours**, so an external engine (OpenSearch/Meilisearch) can back the
> `SearchIndex` port later and rank identically. Backend only.

---

## Tenant isolation is not optional

The #1 search vulnerability is returning another tenant's document. Here the tenant is
taken **from the caller's identity, server-side** — a request body cannot widen the scope.
Every candidate is checked against the caller's tenant before it can score. (Tested:
another tenant's docs are never returned, and a body `tenant` field is ignored.)

## No query injection

A caller sends `text` + **structured filters**; we build the query. There is **no raw
backend query string** to inject into (no Lucene/ES/SQL passthrough, no `eval`) — the same
stance as the authz condition evaluator. Wildcard/regex operator characters are **rejected**
(they're also a DoS vector), and query length + term count are capped.

## Arabic-aware (KSA)

Saudi content is written with and without diacritics and with several identical-looking
letter variants, so `مُحَمَّد` and `محمد`, or `إبراهيم` and `ابراهيم`, are the *same word*.
The analyzer (shared by indexing **and** querying) strips tashkeel/tatweel and folds
`أ إ آ ٱ → ا`, `ى → ي`, `ة → ه`, plus Unicode NFKC + case-fold. Without this, search
silently fails for half an Arabic corpus.

## Relevance & results

- **BM25** ranking with per-field **boosts** (a title match outranks a body match); ties
  break by id so ranking is a total order.
- **Facets** (counts per field value), **highlighting** (`<em>` snippets), structured
  **filters**.
- **Cursor pagination** — an opaque cursor over `(score desc, id asc)`, not an offset, so
  pages don't skip or duplicate rows when the index changes between requests.
- **Typo-tolerant autocomplete** — `suggest` matches prefixes and edit-distance-≤1 near
  misses.

## Hot path

`search` is the read-hot path, so results are cached by
`(tenant, generation, query, filters, facets, sort, cursor, size)`, invalidated by a
**per-tenant generation** bumped on any index write. A repeated query does **0 index reads**
after warm (benchmark-asserted); a query reads only its terms' posting lists, never the
whole corpus. A backend outage returns `unavailable`, not a 500.

## Ports

| Port | Drivers | Role |
|------|---------|------|
| `SearchIndex` | `builtin` (inverted index over DataStore) · `null` | store postings + fetch docs |

Reuses `DataStore`, `Cache` (hot path), `EventBus`.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/search/index` · `/bulk` · `/delete` | any service indexes its entities |
| user | `POST /api/v1/search` | search (tenant from identity) |
| user | `GET /api/v1/search/suggest` | autocomplete (prefix + typo) |
| user | `GET /api/v1/search/indexes/{index}/stats` | index stats (admin) |

Caps: `platform.search` (cross-tenant) · `search.manage` · `search.read`.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (documented follow-ups)
**OpenSearch / Meilisearch** backends (drop-in via the `SearchIndex` port — their config
knobs ship *with* the adapter, never before it) · vector/semantic search (an `embedding`
port) · learned ranking · cross-index joins.

## Rate limiting (every request, config-driven)

Sliding-window (GCRA) limiter — **no boundary burst**, and **atomic** (a check-and-increment race
cannot exceed the limit). Every response carries `RateLimit-Limit/Remaining/Reset`; a 429 also
carries `Retry-After`. On a Redis outage it **degrades to a per-pod limiter** (coarser, still
limiting) rather than failing open or 500-ing.

| Choice | Env knob | Options / example |
|--------|----------|-------------------|
| Backend | `RATELIMITER_DRIVER` | `memory`(per-pod) · `redis`(shared) |
| Budget | `RATELIMIT_MAX` / `RATELIMIT_WINDOW_SECONDS` | e.g. `100` / `60` |
| Key | `RATELIMIT_KEY_PARTS` | `subject,ip` · add `tenant`/`route` |
| Per-route budgets | `RATELIMIT_ROUTES` | `[{"prefix","class","limit","window_s","cost"}]` |
| Only count failures | `RATELIMIT_COUNT_STATUSES` | e.g. `401` (a 403 will not lock a user out) |
| On Redis outage | `RATELIMIT_DEGRADE` | `local`(default) · `closed` · `open` |

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
