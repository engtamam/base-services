# tenant-service

> Self-contained, plug-and-play **FastAPI** multi-tenancy service (SaaS checklist §11).
> Provisioning · **pluggable isolation** (shared-row / schema / db) · **domain
> resolution** (subdomain / custom-domain) · white-label branding · per-tenant config
> (feature flags, limits, locale/timezone — incl. KSA `ar` / `Asia/Riyadh` / Hijri) ·
> lifecycle (suspend / reactivate / delete+purge) with **events** · machine-readable
> export. Backend only. Built from `_template` (inherits ports, auth, CWE guard, CI
> gate). **Zero new deps** (DataStore + EventBus ports).

---

## What it is

The **source of truth** other services consult: *how* a tenant is isolated and *who*
a host belongs to. Peers ask, then react to lifecycle **events** — no code coupling.

## Endpoints

| Tier | Method · Path | Cap | Purpose |
|------|---------------|-----|---------|
| internal | `GET /internal/tenants/resolve?host=` | token | host → tenant (+ scope + config) |
| internal | `GET /internal/tenants/{id}` | token | tenant config + scope |
| platform | `POST /api/v1/tenants` | `platform.tenants` | provision |
| platform | `GET /api/v1/tenants` · `GET /{id}` | `platform.tenants` | list / read |
| platform | `PUT /{id}/config` · `PUT /{id}/branding` | `platform.tenants` | update |
| platform | `POST /{id}/suspend` · `/reactivate` · `DELETE /{id}` | `platform.tenants` | lifecycle |
| platform | `GET /{id}/export` | `platform.tenants` | offboarding export |
| tenant | `GET /api/v1/tenant` · `PUT /tenant/config|branding` | `tenant.manage` | **own tenant only** |

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Isolation | `ISOLATION_DRIVER` (per-tenant override) | `shared-row` · `schema` · `db` |
| Domain → tenant | `DOMAIN_RESOLVER_DRIVER` | `subdomain` · `custom-domain` |
| Base domain | `BASE_DOMAIN` | e.g. `apps.example.com` |
| Storage / events | `DATASTORE_DRIVER` / `EVENTBUS_DRIVER` | memory · postgres / memory · redis |
| Default locale/tz/calendar | `DEFAULT_LOCALE` / `DEFAULT_TIMEZONE` / `DEFAULT_CALENDAR` | KSA: `ar` / `Asia/Riyadh` / `hijri` |

**Isolation scope** returned to peers: `shared-row` → `{namespace: <id>}`, `schema` →
`{schema: t_<slug>}`, `db` → `{dsn: <template with {slug}>}`. Peers scope their own
storage accordingly — tenant-service does not shard.

## Lifecycle events (EventBus)

`tenant.created` · `tenant.updated` · `tenant.suspended` · `tenant.reactivated` ·
`tenant.deleted`. Peers subscribe to purge/suspend their own data on delete/suspend.

## Security

Platform-admin capability for cross-tenant ops; tenant-admin **scoped to own tenant**
(can never read another); slug/host validation; deny-by-default; internal resolve
behind a timing-safe token; CWE respond-guard.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

Actual schema/db creation (peers do it from the returned scope) · branded login UI
(consumer's job) · usage-metering enforcement (billing) · DNS/TLS provisioning (infra).
