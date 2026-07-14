# feature-flag-service

> Self-contained, **general & plug-and-play** runtime **control plane** (SaaS checklist
> §17): **feature flags** (per-tenant + per-user overrides, **deterministic % rollout**,
> stale-flag cleanup), **versioned system configuration** (history + rollback + env
> overrides), and **maintenance mode** (global, **partial per-module**, scheduled
> windows). One call answers every flag for a request context. Backend only.
> **Zero new runtime deps.**

---

## Own it, or delegate it

The `FlagProvider` port decides where **definitions** live — but the **evaluation engine
is always ours**, so behaviour is identical whichever you plug in.

| Port | Drivers | Role |
|------|---------|------|
| `FlagProvider` | **local** (our store) · unleash · launchdarkly (`[ld]` extra) · null | flag definitions |
| `EntitlementChecker` | null · **billing-http** | a flag may require a plan entitlement |

A provider outage is **fail-safe**: the flag falls back to its default (OFF). `null` turns
everything off.

## Precedence (first match wins — every answer carries a `reason`)

| # | Rule | `reason` |
|---|------|----------|
| 1 | flag unknown | `unknown_flag` (OFF) |
| 2 | archived / master switch off | `kill_switch` |
| 3 | outside its scheduled window | `scheduled` |
| 4 | a prerequisite flag is off | `prerequisite_failed` |
| 5 | required entitlement denied | `entitlement_denied` |
| 6 | user override | `user_override` |
| 7 | tenant override | `tenant_override` |
| 8 | segment match | `segment_match` |
| 9 | targeting rule match | `rule_match` |
| 10 | weighted variant assigned | `variant_rollout` |
| 11 | in the rollout bucket | `rollout` |
| 12 | otherwise | `default` |

Reasons make flags debuggable — *"why was it on for that user?"* is always answerable.

## Deterministic rollout (sticky, no per-user table)

```
bucket = sha256(salt : flag_key : subject) mod 10000     → in rollout if bucket < pct·100
```

The same subject always lands in the same bucket — **no flicker** across restarts or
replicas, and **no storage**. The flag key salts the digest, so two 50% flags don't hit
the *same* half of your users. Changing `ROLLOUT_SALT` re-buckets everyone.

**`bucket_by` decides *what* gets hashed** — and getting it wrong is a real footgun:

| `bucket_by` | Subject | Use for |
|-------------|---------|---------|
| `user` (default) | `ctx.user` | UI tests, per-person experiments |
| `tenant` | `ctx.tenant` | **schema / infra rollouts** — every user of a company sees the same thing |
| `attr:<name>` | `ctx.attrs[name]` | device, session, region … |

A missing subject → **not in rollout** (fail-safe), never "half on".

## Experiments: weighted variants + exposure events

```json
{"key": "checkout_exp", "rollout_pct": 100, "variants": {"control": 50, "treatment": 50}}
```

Variants are assigned from the **same deterministic bucket** — sticky, zero storage,
weights validated to sum to 100. Each evaluation emits **`flag.exposed`**
(`{key, variant, subject, bucket_by, reason}`, sampled by `EXPOSURE_SAMPLE_PCT`,
fire-and-forget) so analytics-service can actually **measure** the experiment. Without
exposures, an A/B test is decorative.

## Segments, prerequisites, scheduling

- **Segments** — a named reusable audience (`beta_users`) referenced by many flags, so
  targeting rules are defined once instead of copy-pasted.
- **Prerequisites** — `prerequisites: [{key, variant?}]`: flag B stays off unless flag A
  is on (and optionally serving a given variant). Cycles and missing parents **fail closed**;
  depth is capped by `PREREQUISITE_MAX_DEPTH`.
- **Scheduling** — `start`/`end` on a flag; outside the window it serves its default with
  reason `scheduled`.

## Hot path (this runs on every request)

Flag evaluation is the highest-QPS endpoint in the platform, so it must not touch the
database. Definitions are cached for `FLAG_CACHE_TTL_SECONDS` (busted on every write), and
the `last_evaluated` stamp — which only feeds the stale-flag report — is persisted at most
once per `LAST_SEEN_WRITE_THRESHOLD_SECONDS` per flag via a cache marker.

```
1000 × evaluate_all(40 flags)  →  0 datastore writes, 0 reads
```

A cache outage never breaks evaluation (it falls through to the provider, never to "off").
Two tests guard this: a **hot-path benchmark** and a **config-knob guard** that fails if any
`Settings` field has no consumer.

## Targeting rules (safe by construction)

`{"attribute": "country", "op": "in", "value": ["SA","AE"]}` — conditions are AND-ed,
first matching rule wins. Operators are an **allowlist** (`equals`, `in`, `contains`,
`prefix`, `suffix`, `gt/gte/lt/lte`, …). **No `regex`, no `eval`** — no ReDoS, no code
execution. An unknown attribute or operator never matches (fail-closed).

## Versioned configuration

`PUT` **appends** a version — it never overwrites. `rollback` appends a *new* version
holding the old content, so **history is never rewritten** (auditable, like our
audit-service chain). An env-scoped entry (`CONFIG_ENV`) wins over the global one.
Changes emit `config.changed` / `config.rolled_back` on the EventBus.

## Maintenance mode

Global **or per-module** (partial), with a **scheduled window** (`start`/`end`) and a
message. Global implies every module. Any service or the gateway polls
`GET /internal/maintenance?module=…` to serve a graceful response.

A **bypass allowlist** (`MAINTENANCE_BYPASS_SUBJECTS`) lets on-call admins and health
checks reach the system *during* maintenance — otherwise you can't fix the thing you took
down to fix.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/flags/evaluate` | **bulk** — every flag for one context |
| internal | `GET /internal/flags/{key}` | single flag |
| internal | `GET /internal/config/{key}` · `GET /internal/maintenance` | config / maintenance |
| user | `POST/GET /api/v1/flags` · `DELETE /flags/{key}` (archive) | catalogue |
| user | `POST /api/v1/flags/{key}/overrides` · `DELETE` | tenant / user overrides |
| user | `POST/GET /api/v1/segments` · `DELETE /segments/{key}` | reusable audiences |
| user | `GET /api/v1/flags/stale?days=N` | cleanup worklist |
| user | `POST /api/v1/flags/evaluate` | evaluate |
| user | `PUT/GET /api/v1/config/{key}` · `/history` · `/rollback` | versioned config |
| user | `PUT/GET /api/v1/maintenance` | maintenance mode |

Caps: `platform.flags` (cross-tenant) · `flags.manage` (own tenant) · `flags.read` (read-only).

## Security

Deny-by-default + capability RBAC; a tenant admin **cannot override another tenant**;
internal tier needs `X-Internal-Token`; **fail-safe** flags (unknown/provider-down → OFF)
and **fail-closed** entitlements (billing unreachable → denied, never silently unlocks a
paid feature); no regex/eval in rules; change events for the audit trail.

## Billing tie-in

A flag with `requires_entitlement: "export"` asks billing-service
`/internal/billing/entitlements/check`. **Flags answer "is this code path on"; entitlements
answer "does this plan allow it"** — billing stays the source of truth.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Change audit

`flag.changed` / `flag.archived` carry **`actor` + `before`/`after`**, so audit-service can
answer *"who turned this on, and from what?"*. Config emits `config.changed` /
`config.rolled_back`.

## Out of scope

A/B experiment **statistics** (we emit exposures; analytics-service computes) · **streaming**
flag push (poll + cache TTL + `flag.changed` events) · approval workflows · in-app
**announcements** → notification-service · the **super-admin panel** → UI.

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
