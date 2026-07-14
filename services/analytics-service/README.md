# analytics-service

> Self-contained, plug-and-play **FastAPI** service. Backend + endpoints only (no UI).

Product analytics for any SaaS: event ingest, pre-aggregated rollups, metric queries, funnels,
retention cohorts, saved reports, and CSV/warehouse export.

Hexagonal (every dependency is a port chosen by config), deny-by-default, zero-hardcoded.

---

## Why this is not "just count the events"

Three things break every naive analytics service. This one is built around them:

| Trap | What naive code does | What this does |
| --- | --- | --- |
| **Uniques don't add** | stores `daily_uniques`, sums them for the week | stores a mergeable **HyperLogLog** per bucket, unions them, estimates once |
| **Percentiles don't average** | stores `p95` per hour, averages for the day | stores a mergeable **histogram** per bucket, merges, takes the quantile once |
| **At-least-once delivery** | double-counts a redelivered event | ingest is **idempotent by `event_id`**; a duplicate does zero writes |

Summing 1 000 + 1 000 daily uniques when 500 users returned gives 2 000. The truth is 1 500.
Averaging a quiet hour's p95 with an incident hour's p95 gives a number that is not a
percentile of anything. Both failures are silent — the dashboard looks fine and the number is
wrong.

The unique sketch is **exact** below ~1 024 distinct values per cell (sparse mode) and only
then promotes to the approximate dense form, so small tenants never see estimator noise.

## Hot paths

* **Ingest (write-heavy).** Exactly 1 event write + N rollup updates, **zero scans**, at any
  table size. A duplicate does **zero** additional writes.
* **Query (read-heavy).** Reads **rollup rows only — never the raw event table** — and is
  cached by a per-tenant generation counter, so a repeated query does **zero datastore reads**
  while any write invalidates it immediately.

Both are pinned by benchmarks in `tests/unit/test_hot_path.py`, alongside the guard that every
config knob has a real consumer.

`funnel` and `retention` are sequence questions and *do* read raw events, bounded by the query
range. They are deliberately not on the hot path.

## Correctness rules baked in

* Events bucket by **`event_time`** (when it happened), never ingest time.
* Buckets are always **UTC**; a tenant timezone is applied at **render** time only — no DST
  double-counting, and moving a tenant's timezone never rewrites history.
* An event later than `LATE_EVENT_MAX_SECONDS` is counted under `<metric>.late` — never
  dropped, never silently merged into an already-reported bucket.
* A dimension past `MAX_DIMENSION_CARDINALITY` spills to `__other__`; totals stay exact.
* Known-PII property keys are **dropped** unless allow-listed via `PII_ALLOWED_PROPERTIES`.
* The query DSL is structured and allowlisted — no expression language, nothing to inject.
  Unknown aggregations, unmaterialised granularities, oversized ranges and too many groups are
  **refused**, not approximated.
* CSV export neutralises formula injection (CWE-1236).
* Tenant isolation is enforced in the route, never trusted from the caller (CWE-639).

## Endpoints

Internal (`X-Internal-Token`, compared in constant time):

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/internal/analytics/ingest` | record one event (idempotent by `id`) |
| POST | `/internal/analytics/ingest/batch` | record up to 1 000 events |
| POST | `/internal/analytics/prune-raw` | apply `RAW_RETENTION_DAYS` (rollups persist) |
| GET | `/internal/manifest` | self-description: capabilities, endpoints, ports |

User (`Bearer` JWT + capability, deny-by-default):

| Method | Path | Capability |
| --- | --- | --- |
| POST | `/api/v1/analytics/query` | `analytics.read` |
| POST | `/api/v1/analytics/funnel` | `analytics.read` |
| POST | `/api/v1/analytics/retention` | `analytics.read` |
| POST | `/api/v1/analytics/export` | `analytics.read` |
| GET / POST | `/api/v1/analytics/reports` | read / `analytics.manage` |
| GET / DELETE | `/api/v1/analytics/reports/{id}` | read / `analytics.manage` |
| POST | `/api/v1/analytics/reports/{id}/run` | `analytics.read` |

`platform.analytics` may target any tenant; every other principal is pinned to its own.
Any service on the event bus can feed `/internal/analytics/ingest` directly — pass the event's
stable id and redelivery is free.

## Aggregations

`count`, `sum`, `avg` (weighted across buckets, never a mean of means), `unique` (HLL union),
`percentile` (histogram merge, `0 < percentile < 1`).

Granularities: `hour`, `day`, `month` — exactly what ingest materialises. Asking for `minute`
returns **400**, because answering `0` from rollup rows that were never written would be a lie.

## Configuration

Adapters: `WAREHOUSE_DRIVER` (`null` | `csv`), `CLOCK_DRIVER` (`system` | `manual`), plus the
standard datastore / cache / event bus / queue / object store / secrets / token-verifier /
rate-limiter / telemetry ports. The `null` warehouse returns nothing and `/export` answers
**501** rather than handing back an empty file that looks like real data.

Behaviour: `QUERY_CACHE_TTL_SECONDS`, `MAX_DIMENSION_CARDINALITY`, `LATE_EVENT_MAX_SECONDS`,
`DEFAULT_TIMEZONE_OFFSET_MINUTES` (180 = KSA UTC+3, rendering only), `MAX_QUERY_RANGE_DAYS`,
`MAX_GROUPS`, `HLL_PRECISION`, `HISTOGRAM_BUCKETS`, `PII_ALLOWED_PROPERTIES`,
`RAW_RETENTION_DAYS`, `ANALYTICS_LIST_LIMIT`.

An unknown driver fails fast at boot with `ConfigError` (the registry is the allowlist).

## Run

```bash
ENV=development INTERNAL_SERVICE_TOKEN=<40+ chars> WAREHOUSE_DRIVER=csv \
  python -m uvicorn app.main:app --port 8000

python -m pytest -q          # 189 tests
python ci/security_gate.py   # license / SAST / secrets / CVE
```

## Example

```bash
# a service reports an event (its own stable id makes redelivery free)
curl -X POST localhost:8000/internal/analytics/ingest \
  -H "X-Internal-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"id":"evt_9f3","name":"checkout.completed","tenant":"acme",
       "subject":"user_42","value":249.0,"props":{"plan":"pro"}}'

# weekly unique users — a real union, not a sum of daily counts
curl -X POST localhost:8000/api/v1/analytics/query \
  -H "Authorization: Bearer $JWT" -H 'content-type: application/json' \
  -d '{"metric":"checkout.completed","agg":"unique","granularity":"day",
       "start_ts":1772323200,"end_ts":1772928000}'
```

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
