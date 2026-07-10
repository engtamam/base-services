# Phase 16 — feature-flag-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** runtime **control plane** (SaaS checklist
> §17): **feature flags** (per-tenant + per-user overrides, **deterministic % rollout**,
> stale-flag cleanup), **system configuration** (versioned, env-scoped, audited,
> rollback), and **maintenance mode** (graceful, **partial per-module**, scheduled
> windows). One `evaluate` call answers every flag for a request context.
>
> It can **own** the flags (local store) or **delegate** to Unleash/LaunchDarkly behind
> one `FlagProvider` port — so any project drops it in either way. A flag may also
> **require a plan entitlement**, asking billing-service through an `EntitlementChecker`
> port (the flag↔billing tie-in, mirroring billing↔zatca).
>
> _Pairs with [[billing-service]] (entitlements), tenant, audit (change log), notification (announcements)._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 16)

- [ ] `services/feature-flag-service` generated from `_template` (self-contained).
- [ ] **`FlagProvider` port** — `local` (our store, default) · `unleash` · `launchdarkly` · `null` (fail-closed, everything off).
- [ ] **`EntitlementChecker` port** — `null` (allow) · `billing-http` (asks billing `/internal/billing/entitlements/check`).
- [ ] **Flags** — create/update/archive; **boolean**, **percentage rollout**, **targeting rules**; per-**tenant** and per-**user** overrides.
- [ ] **Deterministic rollout** — `hash(flag_key + subject)` → the same subject always gets the same answer (no flicker), salted per flag so flags don't correlate.
- [ ] **Documented precedence** — kill-switch → user override → tenant override → rule match → % rollout → default. First-match-wins, fail-safe default.
- [ ] **Bulk `evaluate`** — one internal call returns every flag for a `{tenant, user, attrs}` context (cached).
- [ ] **Stale-flag cleanup** — track `created` + `last_evaluated`; `GET /flags/stale?days=N` lists flags to retire.
- [ ] **System config** — versioned put/get, history, **rollback to version**, env-scoped overrides, change events (audit).
- [ ] **Maintenance mode** — global + **per-module**, scheduled windows (start/end), message; internal `GET /maintenance` any service/gateway can poll.
- [ ] **Security** — deny-by-default, capability RBAC (`platform.flags` / `flags.manage` / `flags.read`), tenant-scoped overrides, change events emitted for audit, secret-free.
- [ ] **Zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot (flag on/off, % rollout stickiness, tenant/user override, entitlement-gated flag, config rollback, maintenance window).

---

## 1. Prerequisites & decisions

1. **Evaluation is pure** — a `context` in, a decision + **reason** out (`user_override`, `rollout`, `default`…). Reasons make flags debuggable; auditors and support need "why was it on for this user?".
2. **Fail-safe, not fail-open** — an unknown flag / unreachable provider returns the flag's configured **default** (`off` unless stated). The `null` provider turns everything off.
3. **Sticky by hash, not by storage** — no per-user rollout table; `sha256(salt + key + subject) % 10000` gives a stable bucket. Deterministic across restarts and replicas.
4. **Config is versioned, never overwritten** — every put appends a version; rollback creates a *new* version pointing at old content (append-only, like our audit chain).
5. **Flags ≠ entitlements** — a flag says "is this code path on"; an entitlement says "does this plan allow it". A flag may *require* an entitlement; billing stays the source of truth.

---

## 2. Steps

### Step 16.1 — Skeleton
- `python scripts/create_service.py feature-flag-service --desc "Feature flags, versioned config & maintenance mode"`. Zero new deps → reuse template venv. Confirm inherited tests + gate green.

### Step 16.2 — Config (zero-hardcoded)
- `FLAG_PROVIDER_DRIVER` (local), `UNLEASH_URL`/`UNLEASH_TOKEN`, `LAUNCHDARKLY_SDK_KEY`; `ENTITLEMENT_CHECKER_DRIVER` (null) + `BILLING_SERVICE_URL`/`BILLING_SERVICE_TOKEN`; `ROLLOUT_SALT`; `FLAG_CACHE_TTL_SECONDS`; `STALE_FLAG_DAYS`; `CONFIG_ENV` (env-scoped overrides); `FLAG_LIST_LIMIT`. `.env.example`.

### Step 16.3 — Ports
- `FlagProvider` (list/get/evaluate-source), `EntitlementChecker` (allowed(tenant, key)). No third-party imports in ports.

### Step 16.4 — Adapters + registry
- Flags: `local` (DataStore) · `unleash` (stdlib HTTP, lazy) · `launchdarkly` (lazy `[ld]` extra) · `null` (all-off). Entitlement: `null` · `billing-http` (stdlib POST, `X-Internal-Token`). Registry allowlist + fail-fast `ConfigError`.

### Step 16.5 — Core: evaluation engine (pure)
- `rollout.py` — `bucket(salt, key, subject) -> 0..9999`, `in_rollout(pct)`.
- `rules.py` — attribute matchers (`equals`, `in`, `contains`, `regex`?, `gt/lt`), safe (no eval, ReDoS-safe).
- `evaluate.py` — precedence chain → `{enabled, reason, variant}`.

### Step 16.6 — Domain models + stores
- `Flag` (key, description, enabled, default, rollout_pct, rules[], variants, tenant_overrides, user_overrides, archived, created, last_evaluated), `ConfigEntry` + `ConfigVersion`, `Maintenance` (global/module, start, end, message). Stores over `DataStore`.

### Step 16.7 — Core FlagService
- CRUD + archive; set tenant/user override; set rollout; `evaluate(key, ctx)` and `evaluate_all(ctx)` (cached, touches `last_evaluated`); `stale(days)`; entitlement-gated flags; emit `flag.changed` on EventBus.

### Step 16.8 — Core ConfigService + Maintenance
- Versioned `put/get/history/rollback`, env-scoped overrides (`CONFIG_ENV` wins over global), change events. Maintenance get/set (global + per-module + scheduled window), `active_now()`.

### Step 16.9 — Routes + manifest
- internal: `POST /internal/flags/evaluate` (bulk), `GET /internal/flags/{key}` (single), `GET /internal/config/{key}`, `GET /internal/maintenance`.
- user/admin (`platform.flags`/`flags.manage`): flags CRUD, overrides, rollout, `GET /flags/stale`; config CRUD + `/history` + `/rollback`; maintenance set. `flags.read` for read-only.
- rate_limit, deny-by-default, guard respond. Manifest.

### Step 16.10 — Tests
- Unit: bucket determinism + uniform distribution, precedence chain (each reason), rule matchers, unknown flag → default, null provider all-off, entitlement gate, config version/rollback, maintenance window (before/during/after).
- Integration: CRUD → evaluate → override → rollout stickiness → stale list; config put/history/rollback; maintenance; cap-gating; tenant scope.
- Security: deny-by-default, no ReDoS in matchers, unknown flag fail-safe, override can't cross tenant.

### Step 16.11 — Verify + live boot
- `pytest` + gate green. Live: flag off→on, same user sticky across calls, tenant+user override precedence, entitlement-gated flag denied without plan, config rollback, maintenance window activates.

### Step 16.12 — Docs
- README (ports, precedence table, rollout math, stale cleanup, config versioning, maintenance, billing tie-in). Update architecture-plan + build-progress memory.

---

## 3. Out of scope (deferred)

- **A/B experiment analytics** (flags emit events; analytics-service measures).
- **Streaming flag updates** (SSE/websocket push) — poll + cache TTL + `flag.changed` events for now.
- **In-app announcements** → notification-service. **Super-admin panel** → UI.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #166 | 16.1 Skeleton | ✅ completed |
| #167 | 16.2 Config | ✅ completed |
| #168 | 16.3 Ports | ✅ completed |
| #169 | 16.4 Adapters + registry | ✅ completed |
| #170 | 16.5 Evaluation engine (pure) | ✅ completed |
| #171 | 16.6 Domain models + stores | ✅ completed |
| #172 | 16.7 Core FlagService | ✅ completed |
| #173 | 16.8 ConfigService + Maintenance | ✅ completed |
| #174 | 16.9 Routes + manifest | ✅ completed |
| #175 | 16.10 Tests | ✅ completed |
| #176 | 16.11 Verify + live boot | ✅ completed |
| #177 | 16.12 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (165 tests; gate PASS; live verified) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is general + smart

| Choice | Knob | Options |
|---|---|---|
| Flag source | `FLAG_PROVIDER_DRIVER` | local · unleash · launchdarkly · null |
| Entitlement gate | `ENTITLEMENT_CHECKER_DRIVER` | null · billing-http |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Cache | `CACHE_DRIVER` | memory · redis |

**Precedence (first match wins):**

| # | Rule | Reason |
|---|------|--------|
| 1 | flag archived / kill-switch off | `kill_switch` |
| 2 | entitlement required & denied | `entitlement_denied` |
| 3 | user override | `user_override` |
| 4 | tenant override | `tenant_override` |
| 5 | targeting rule match | `rule_match` |
| 6 | percentage rollout bucket | `rollout` |
| 7 | otherwise | `default` |

Every decision returns a **reason** — so "why is this on for that user?" is always answerable.

_When every box is ✅, feature-flag-service is live — the runtime control plane for any project._
