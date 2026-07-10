# Phase 8 — tenant-service (detailed steps)

> **Goal:** the Tier-1 **tenant-service** (SaaS checklist §11). Manages tenants:
> provisioning, **pluggable isolation strategy** (shared-row / schema / db),
> **domain resolution** (subdomain / custom-domain), white-label branding,
> per-tenant config (feature flags, limits, locale/timezone — incl. KSA
> Arabic/RTL/Hijri), lifecycle (suspend / reactivate / delete+purge) with events,
> and machine-readable offboarding export. Plug-and-play, config-driven,
> **zero new deps** (DataStore + EventBus ports only).
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §11._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 8)

- [ ] `services/tenant-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Pluggable by config:** `IsolationStrategy` (shared-row / schema / db) resolves a tenant's storage scope; `DomainResolver` (subdomain / custom-domain) maps a host → tenant. All state via `DataStore`; lifecycle events via `EventBus`.
- [ ] **Provisioning** — create tenant (unique slug), default config, initial domains, `pending`→`active`.
- [ ] **Domain resolution** — `GET /internal/tenants/resolve?host=` → tenant (subdomain or mapped custom domain).
- [ ] **White-label + config** — per-tenant branding (logo, colors, sender) + config (feature flags, usage limits, locale, timezone); updatable.
- [ ] **Lifecycle** — suspend / reactivate / delete(+purge own data); each **emits an event** (`tenant.created|suspended|reactivated|deleted`) so peers react (e.g. purge). Suspended tenant flagged so peers can block.
- [ ] **Offboarding** — `GET /{id}/export` returns a full machine-readable JSON of the tenant; delete confirms purge + emits event + audit.
- [ ] **Security** — platform-admin capability for cross-tenant ops; tenant-admin scoped to own tenant; slug/host validation; no cross-tenant leakage; CWE respond-guard.
- [ ] **Zero new deps**; `pytest` + `ci/security_gate.py` green; live boot proven (provision → resolve → suspend → export).

---

## 1. Prerequisites & decisions

1. **Zero new deps.** Only the existing `DataStore` + `EventBus` ports. `IsolationStrategy`/`DomainResolver` are pure logic.
2. **Isolation strategy is advisory metadata.** tenant-service stores each tenant's isolation `mode` and returns the resolved **scope** (`{mode, namespace|schema|dsn}`); *peers* use it to scope their own storage. tenant-service does not itself shard — it is the source of truth other services consult.
3. **Events, not direct calls.** Lifecycle changes emit domain events (EventBus); peers subscribe to purge/suspend. Keeps services decoupled.
4. **KSA-aware config.** Per-tenant `locale` (e.g. `ar`), `timezone` (e.g. `Asia/Riyadh`), and a `calendar` hint (`gregorian|hijri`) are first-class config fields — generic, not hardcoded.

---

## 2. Steps

### Step 8.1 — Generate skeleton
- `python scripts/create_service.py tenant-service --desc "Multi-tenancy: provisioning, isolation, lifecycle"`.
- No new deps → verify reuses the template venv. Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 8.2 — Config (zero-hardcoded)
- `DEFAULT_ISOLATION` (`shared-row`), `BASE_DOMAIN` (for subdomain resolution, e.g. `apps.example.com`), `DEFAULT_LOCALE` (`en`), `DEFAULT_TIMEZONE` (`UTC`), `SLUG_MIN/MAX_LEN`, `TENANT_LIST_LIMIT`. Update `.env.example`.
- **Check:** boots with defaults; fail-fast on bad values.

### Step 8.3 — Ports
- `app/ports/isolation_strategy.py` (`scope(tenant) -> dict` = `{mode, namespace|schema|dsn}`), `app/ports/domain_resolver.py` (`tenant_slug_for(host) -> str|None`).
- **Check:** import pulls no third-party driver.

### Step 8.4 — Adapters + registry
- Isolation: `shared_row.py` (namespace = tenant id), `schema.py` (schema = `t_<slug>`), `db.py` (dsn from a template + tenant). Resolver: `subdomain.py` (strip BASE_DOMAIN → slug), `custom_domain.py` (look up a domain→slug map in DataStore, fallback to subdomain).
- Extend `container.py` with `isolation`, `domain_resolver` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; defaults build; subdomain resolver strips base domain.

### Step 8.5 — Domain: model + repository
- `core/models.py` `Tenant` (id, slug, name, status[pending|active|suspended|deleted], isolation_mode, domains[], branding{logo,colors,email_sender}, config{locale,timezone,calendar,feature_flags{},limits{}}, custom_terms, created/updated).
- `core/tenant_repository.py` over `DataStore` (by id + **slug index** + **domain index**; uniqueness); tenant-scoped queries.
- **Check:** create/get/by-slug/by-domain/uniqueness.

### Step 8.6 — Core TenantService
- `core/tenant_service.py`: `provision`, `get`, `resolve_by_host`, `update_config`, `set_branding`, `suspend`, `reactivate`, `delete`(purge own records), `export`(full JSON), `scope`(via IsolationStrategy). Emit lifecycle events on the EventBus.
- **Check:** unit tests: provision uniqueness, resolve, lifecycle transitions + events, export shape, isolation scope per mode.

### Step 8.7 — Routes
- Platform admin (`require_capability("platform.tenants")`): `POST /api/v1/tenants`, `GET /api/v1/tenants`, `GET /api/v1/tenants/{id}`, `PUT /api/v1/tenants/{id}/config`, `PUT /api/v1/tenants/{id}/branding`, `POST /api/v1/tenants/{id}/suspend|reactivate`, `DELETE /api/v1/tenants/{id}`, `GET /api/v1/tenants/{id}/export`.
- Tenant admin (`require_capability("tenant.manage")`, scoped to own `principal.tenant`): `GET /api/v1/tenant`, `PUT /api/v1/tenant/branding|config`.
- Internal: `GET /internal/tenants/resolve` (host→tenant), `GET /internal/tenants/{id}` (config for scoping).
- Wire `rate_limit`, deny-by-default, `guard.step("respond")`.
- **Check:** routes mount; cross-tenant op requires platform cap; tenant-admin can't touch another tenant.

### Step 8.8 — Manifest & wiring
- Manifest capabilities (`platform.tenants`, `tenant.manage`) + endpoints; ports listed (isolation/domain_resolver drivers).
- **Check:** `/internal/manifest` lists tenant ports + endpoints.

### Step 8.9 — Tests
- Unit: repository (slug/domain uniqueness), provision, resolve_by_host (subdomain + custom), lifecycle + emitted events, export, isolation scope per mode.
- Integration: provision → get → resolve (internal) → suspend → export; tenant-admin scoped (can read own, 403 on another); platform-cap required.
- Security: slug/host validation, **no cross-tenant leakage**, deny-by-default, suspended flag surfaces.
- **Check:** all green.

### Step 8.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: provision a tenant (slug `acme`) → `GET /internal/tenants/resolve?host=acme.<BASE_DOMAIN>` returns it → suspend → export returns full JSON.
- **Check:** end-to-end works.

### Step 8.11 — Docs
- Service `README.md`: what it is, endpoint table, config knobs, plug-and-play isolation/domain matrix, lifecycle events, KSA config notes.
- Update `docs/architecture-plan.md` (tenant ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **Actual data sharding** (schema/db creation) — peers implement using the scope tenant-service returns.
- **Branded login UI** — UI is the consumer's job; we serve branding data.
- **Usage metering enforcement** — billing/subscription service (Tier 2).
- **Domain DNS/TLS provisioning** — infra concern.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #76 | 8.1 Skeleton | ✅ completed |
| #77 | 8.2 Config | ✅ completed |
| #78 | 8.3 Ports | ✅ completed |
| #79 | 8.4 Adapters + registry | ✅ completed |
| #80 | 8.5 Domain model + repository | ✅ completed |
| #81 | 8.6 Core TenantService | ✅ completed |
| #82 | 8.7 Routes | ✅ completed |
| #83 | 8.8 Manifest & wiring | ✅ completed |
| #84 | 8.9 Tests | ✅ completed |
| #85 | 8.10 Verify + live boot | ✅ completed |
| #86 | 8.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (134 tests; gate PASS; live boot OK) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play (the point)

| Choice | Knob | Options |
|---|---|---|
| Isolation model | `DEFAULT_ISOLATION` (per-tenant override) | shared-row · schema · db |
| Domain → tenant | `DomainResolver` driver | subdomain · custom-domain |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Events | `EVENTBUS_DRIVER` | memory · redis |
| Default locale/timezone | `DEFAULT_LOCALE` / `DEFAULT_TIMEZONE` | any (KSA: `ar` / `Asia/Riyadh`) |

Peers ask tenant-service *how* to scope (isolation) and *who* a host belongs to
(resolve), then react to lifecycle events — no code coupling.

_When every box is ✅, tenant-service is live. Next Tier-1: authz · api-gateway · file · audit._
