# Phase 10 — audit-service (detailed steps)

> **Goal:** the Tier-1 **audit-service** — the platform's central **append-only
> audit log** (SaaS checklist §1 identity audit · §17 admin action log · §6 SIEM ·
> §7 retention). Smart & plug-and-play: a **pluggable integrity strategy**
> (none / **hash-chain** / **hmac-chain** = tamper-evidence), **pluggable exporters**
> (json / csv / ndjson), ingest via `/internal/audit` (+ optional EventBus subscribe),
> storage over the generic `DataStore`, and an **integrity-verify** endpoint that
> detects any modification or deletion. Zero-hardcoded, zero new deps.
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §1/§6/§7/§17._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 10)

- [ ] `services/audit-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Append-only** — events are recorded, never updated/deleted (except a policy-driven retention purge, which itself is audited).
- [ ] **Pluggable integrity:** `IntegrityStrategy` (none / hash-chain / hmac-chain) seals each event by chaining `prev_hash` → **tamper-evident**. A verify pass recomputes the chain and flags any break.
- [ ] **Pluggable export:** `AuditExporter` (json / csv / ndjson).
- [ ] **Ingest:** `POST /internal/audit` (X-Internal-Token) is the primary path; an optional EventBus subscription (config topics) records events when a shared bus (redis) is used.
- [ ] **Query** with filters (service, actor, action, resource, tenant, time range) + pagination; **tenant-scoped** for tenant admins, all for platform.
- [ ] **Integrity verify** endpoint recomputes the chain and returns `ok` + first tampered seq (if any).
- [ ] **Security:** platform-admin capability for cross-tenant queries; tenant-admin scoped; ingest behind timing-safe token; no PII over-collection; safe errors.
- [ ] **Zero new deps · zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot proven (record → query → verify intact → tamper → verify detects it).

---

## 1. Prerequisites & decisions

1. **Zero new deps.** Hashing is stdlib `hashlib`/`hmac`; storage is the `DataStore` port; ingest is HTTP + the existing `EventBus`.
2. **Chain semantics.** Each event gets a monotonic `seq` and `hash = H(prev_hash || canonical(event))`. The chain is per **partition** (default: global; optionally per-tenant) — config-driven. Verify walks the chain.
3. **Append-only is a contract**, enforced in the service (no update/delete API); retention purge is the only removal and is itself recorded + re-anchors the chain from the purge point.
4. **Cross-service ingest** is HTTP push (`/internal/audit`) so it works with the memory default; EventBus subscribe is the shared-bus (redis) enhancement.

---

## 2. Steps

### Step 10.1 — Generate skeleton
- `python scripts/create_service.py audit-service --desc "Central append-only audit log"`.
- No new deps → verify reuses the template venv. Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 10.2 — Config (zero-hardcoded)
- `INTEGRITY_DRIVER` (`hash-chain`), `INTEGRITY_HMAC_KEY` (hmac-chain), `AUDIT_PARTITION` (`global` | `tenant`), `EXPORTER_DEFAULT` (`json`), `RETENTION_DAYS` (0 = keep forever), `AUDIT_SUBSCRIBE_TOPICS` (comma list, empty = none), `AUDIT_QUERY_LIMIT` (100). Update `.env.example`.
- **Check:** boots with defaults; fail-fast on bad values.

### Step 10.3 — Ports
- `app/ports/integrity_strategy.py` (`seal(prev_hash, event) -> str`, `verify(prev_hash, event, expected) -> bool`), `app/ports/audit_exporter.py` (`export(events) -> (content_type, bytes)`).
- **Check:** import pulls no third-party driver.

### Step 10.4 — Adapters + registry
- Integrity: `none.py` (empty hash), `hash_chain.py` (sha256 of `prev||canonical`), `hmac_chain.py` (keyed hmac-sha256). Exporter: `json.py`, `csv.py`, `ndjson.py`.
- Extend `container.py` with `integrity`, `exporter` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; hash-chain seal is deterministic; hmac needs a key.

### Step 10.5 — Domain: model + repository
- `core/models.py` `AuditEvent` (id, seq, ts, service, actor, action, resource, tenant, ip, metadata, prev_hash, hash).
- `core/audit_repository.py` over `DataStore`: `append(event)`, `next_seq(partition)`, `head_hash(partition)`, `query(filters, limit)`, `iterate(partition)` (ordered by seq).
- **Check:** append + monotonic seq + ordered iterate + filtered query.

### Step 10.6 — Core AuditService
- `core/audit_service.py`: `record(evt)` → assign seq, chain hash via `IntegrityStrategy`, append; `query(filters)`; `verify(partition)` → walk chain, return `{ok, checked, first_bad_seq}`; `export(filters, format)`; `purge(before_ts)` (retention; records a `audit.purged` event, re-anchors).
- **Check:** unit: chain builds, tamper (mutate a stored event) → verify flags it, deletion → verify flags gap, query filters, export.

### Step 10.7 — Ingest
- `POST /internal/audit` (token) → `record`. Optional: on startup subscribe to `AUDIT_SUBSCRIBE_TOPICS` on the EventBus; handler maps a domain event → an AuditEvent and records it (inert with memory bus, active with redis).
- **Check:** ingest records; malformed event → 400.

### Step 10.8 — Routes + manifest
- Admin: `GET /api/v1/audit` (query; `platform.audit` cross-tenant, `audit.view` own-tenant), `GET /api/v1/audit/verify` (`platform.audit`), `GET /api/v1/audit/export` (format), `POST /api/v1/audit/purge` (`platform.audit`). Internal: `POST /internal/audit`.
- Wire `rate_limit`, deny-by-default, `guard.step("respond")`. Manifest caps + endpoints + ports.
- **Check:** cross-tenant needs platform cap; tenant-admin scoped.

### Step 10.9 — Tests
- Unit: seq monotonic, hash-chain deterministic + tamper/deletion detected, hmac needs key, exporters (json/csv/ndjson), query filters, purge records + re-anchors.
- Integration: `/internal/audit` (token required; record) → query → verify ok; export; tenant-scoped query (tenant-admin sees only own); platform cap for verify/purge.
- Security: ingest token required, append-only (no update/delete route), tamper detection end-to-end, no cross-tenant leakage, deny-by-default.
- **Check:** all green.

### Step 10.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: record 3 events via `/internal/audit` → `GET /verify` = ok → mutate a stored event directly → `GET /verify` reports `ok=false` + the tampered seq.
- **Check:** end-to-end tamper-evidence works.

### Step 10.11 — Docs
- Service `README.md`: what it is, event shape, integrity/exporter matrix, ingest paths, verify semantics, retention, tenant scoping.
- Update `docs/architecture-plan.md` (audit ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **Real-time SIEM streaming / alerting** → an export Sink adapter later.
- **WORM / external immutable storage** (S3 Object Lock) → an ObjectStore-backed archive adapter later.
- **Anomaly detection** → analytics-service.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #98 | 10.1 Skeleton | ✅ completed |
| #99 | 10.2 Config | ✅ completed |
| #100 | 10.3 Ports | ✅ completed |
| #101 | 10.4 Adapters + registry | ✅ completed |
| #102 | 10.5 Domain model + repository | ✅ completed |
| #103 | 10.6 Core AuditService | ✅ completed |
| #104 | 10.7 Ingest | ✅ completed |
| #105 | 10.8 Routes + manifest | ✅ completed |
| #106 | 10.9 Tests | ✅ completed |
| #107 | 10.10 Verify + live boot | ✅ completed |
| #108 | 10.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (134 tests; gate PASS; tamper-evidence live) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play + smart

| Choice | Knob | Options |
|---|---|---|
| Tamper-evidence | `INTEGRITY_DRIVER` | none · hash-chain · hmac-chain |
| Export format | exporter | json · csv · ndjson |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Ingest events | `AUDIT_SUBSCRIBE_TOPICS` + `EVENTBUS_DRIVER` | HTTP always; redis for bus subscribe |
| Partitioning | `AUDIT_PARTITION` | global · tenant |
| Retention | `RETENTION_DAYS` | int |

The **hash-chain** is the smart bit: any modification or deletion of a past event
breaks the chain and is caught by `verify` — a cheap, dependency-free integrity proof.

_When every box is ✅, audit-service is live. Next Tier-1: authz · file._
