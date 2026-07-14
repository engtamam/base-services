# audit-service

> Self-contained, plug-and-play **FastAPI** central **append-only audit log** (SaaS
> checklist §1 identity audit · §17 admin action log · §6 SIEM · §7 retention).
> Every service records events here; a **tamper-evident hash chain** proves nothing
> was modified or deleted. Backend only. Built from `_template`. **Zero new deps**
> (stdlib `hashlib`/`hmac`; storage via the `DataStore` port).

---

## What it is

Services `POST /internal/audit` their events; the service assigns a monotonic `seq`
and chains `hash = seal(prev_hash, event)`. A `verify` pass walks the chain and
flags the **first tampered or deleted** event. There is **no update/delete API** —
it is append-only; retention purge is the only removal and re-anchors the chain.

## Event shape

`{ id, seq, ts, service, action, actor, resource, tenant, ip, metadata, prev_hash, hash }`

## Endpoints

| Tier | Method · Path | Cap | Purpose |
|------|---------------|-----|---------|
| internal | `POST /internal/audit` | token | record an event (primary ingest) |
| user | `GET /api/v1/audit` | `platform.audit` (all) / `audit.view` (own tenant) | query (filters: service, actor, action, resource, tenant, since, until) |
| user | `GET /api/v1/audit/verify` | `platform.audit` | **integrity check** → `{ok, checked, first_bad_seq}` |
| user | `GET /api/v1/audit/export?format=` | query caps | export (json/csv/ndjson) |
| user | `POST /api/v1/audit/purge` | `platform.audit` | retention purge (re-anchors chain) |

Optional ingest: set `AUDIT_SUBSCRIBE_TOPICS` + a shared `EVENTBUS_DRIVER=redis` and
the service auto-records those bus events.

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Tamper-evidence | `INTEGRITY_DRIVER` | `none` · `hash-chain` · `hmac-chain` (keyed) |
| Export format | `EXPORTER_DEFAULT` | `json` · `csv` · `ndjson` |
| Partitioning | `AUDIT_PARTITION` | `global` · `tenant` |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Bus ingest | `AUDIT_SUBSCRIBE_TOPICS` + `EVENTBUS_DRIVER` | topics; redis for cross-service |
| Retention | `RETENTION_DAYS` | int (0 = keep forever) |

## The smart bit — tamper-evidence

Each event's hash chains from the previous. **Any** modification or deletion of a
past event breaks the chain and is caught by `verify` (returns the first bad seq).
A cheap, dependency-free integrity proof — verified live:

```
3 events → verify ok · mutate event #2 → verify {ok:false, first_bad_seq:2, "content tampered"}
```

`hmac-chain` adds a secret key so only key-holders can produce valid hashes.

## Security

Append-only (no update/delete route); ingest behind a timing-safe token;
platform-admin cap for cross-tenant query/verify/purge; tenant-admin **scoped to own
tenant** (never sees another); deny-by-default; safe errors.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

Real-time SIEM streaming · WORM/external immutable storage (S3 Object Lock) ·
anomaly detection (analytics-service).

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

> The subject is resolved **inside** the limiter (its own optional dependency), not read from
> pre-set request state — so an authenticated request keys per-subject regardless of the route's
> dependency order. No "one office NAT shares a bucket" surprise. Enforced by `safety:edge`.

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
