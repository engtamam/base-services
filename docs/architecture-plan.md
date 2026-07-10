# base-services — Architecture Plan

> **The master plan.** Every service, decision, and rule flows from this document.
> _Status: v0.1 (draft) · Python + FastAPI · target: universal, open-source, commercial-ready._

---

## 0. Mission

Build a catalog of **general-purpose backend services** that plug into **any** SaaS or
platform. Not for one project — for anyone, anywhere, potentially open-sourced for the world.

We learn the good patterns from a reference project ([`ammar` / ProjectBrain], a hardened
TypeScript microservices monorepo) and **do it better**: more generic, more secure, fully
adapter-driven.

We build **backend + HTTP endpoints only**. UI is the consumer's job.

---

## 1. Non-negotiable rules

These are hard constraints. A change that breaks any of them is rejected.

1. **Self-contained — 100%.** Every service is a standalone app. **No shared cross-service
   library.** The `_template` is a *copy-me seed*, not a runtime dependency. Duplication across
   services is intentional and acceptable. A service can be lifted out and dropped into any repo
   and still run.

2. **Everything is an adapter.** Every outside dependency (database, cache, queue, email, storage,
   payment, auth provider…) sits behind an **interface (port)**. Implementations (**adapters**) are
   swappable. This is **Hexagonal / Ports & Adapters** architecture.

3. **Zero hardcoded — everything config-driven.** No hardcoded URLs, hosts, ports, secrets,
   provider names, limits, table names, or business values **anywhere** in code. Behavior — including
   *which adapter is used* — is chosen by config/env at runtime. A service adapts to any platform
   **without code changes**.

4. **Secure by default, no vulns.** Security is built into the template, not bolted on. Deny-by-default
   authorization, validated input, secrets never in code, safe errors, hardened headers.

5. **Config fails fast; infra fails soft.** Bad/missing config → refuse to boot. A missing optional
   backend (Vault, Redis) → fall back gracefully, never crash silently.

6. **Permissive licenses only.** Dependencies must be **MIT / BSD / Apache-2.0 / ISC** (+ PSF).
   **Never** GPL, LGPL, MPL, **AGPL, SSPL, BSL, Elastic, Commons Clause**, or unlicensed packages.
   CI fails the build on any non-allowed license. We ship under **MIT or Apache-2.0**.

7. **Runs with zero infra.** Every port ships a **memory / null adapter**, so a service boots and is
   testable with no database, no Redis, nothing — real adapters are opt-in via config.

8. **Standards-first — avoid CWEs & CVEs by design.** We build to recognized international standards
   (ISO 27001/27002/27701, OWASP ASVS & Top 10, CWE Top 25, NIST SSDF, SLSA). Every service is
   designed to *not contain* the known weakness classes (CWEs), and every dependency is continuously
   scanned so no known vulnerability (CVE) ships. See §9.

---

## 2. The 3-layer model

Everything in the SaaS checklist maps to exactly one of these layers.

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3 — SERVICES        one self-contained FastAPI app     │  ← the product menu
│  (identity, billing, notifications, files, …)                 │
├─────────────────────────────────────────────────────────────┤
│  LAYER 2 — PORTS           swappable interfaces, chosen by cfg │  ← plug-and-play
│  (DataStore, Cache, EventBus, ObjectStore, TokenVerifier, …)  │
├─────────────────────────────────────────────────────────────┤
│  LAYER 1 — TEMPLATE CORE   baked into every service, free      │  ← the skeleton
│  (config, security, observability, health, error handling)    │
└─────────────────────────────────────────────────────────────┘
```

- **Layer 1** = the `_template`. Concerns from checklist **§5 Observability, §6 Security, §10 API
  design, §3 data basics** live here — not as services, as the skeleton.
- **Layer 2** = the universal ports. The knobs that make a service run on anyone's stack.
- **Layer 3** = the runtime services, each cloned from the template.

---

## 3. Hexagonal architecture (per service)

Every service has the same inside shape. The **core never imports an adapter** — it only knows ports.

```
        HTTP (FastAPI routers)              ← driving adapter (inbound)
                 │
                 ▼
        ┌──────────────────┐
        │   CORE / DOMAIN  │   pure logic, no I/O, no framework, no SQL
        │   (services,     │
        │    use-cases)    │
        └──────────────────┘
                 │  depends only on PORTS (interfaces)
     ┌───────────┼───────────┬───────────┐
     ▼           ▼           ▼           ▼
 DataStore    EventBus    ObjectStore  TokenVerifier   ← ports
     │           │           │           │
  [postgres]  [redis]      [s3]        [jwks]          ← driven adapters (outbound), chosen by config
  [memory]    [memory]     [local]     [hs256]           (every port has a memory/null adapter)
```

**Rule:** dependencies point *inward*. Adapters depend on ports; core depends on nothing external.
Swapping Postgres → Mongo, or Redis → NATS, touches **only** an adapter + one config value.

### Standard folder layout (the `_template`)

```
_template/
├── pyproject.toml                # deps (permissive-only), tooling
├── Dockerfile / .dockerignore
├── .env.example                  # every knob documented, no real values
├── README.md                     # how to clone → customize
├── LICENSE                       # MIT / Apache-2.0
├── app/
│   ├── main.py                   # FastAPI app factory + lifespan
│   ├── config.py                 # Pydantic BaseSettings (fail-fast)
│   ├── container.py              # wires ports→adapters from config (registry)
│   ├── core/                     # pure domain logic (no I/O)
│   │   ├── models.py
│   │   └── services.py
│   ├── ports/                    # interfaces (Protocol / ABC)
│   │   ├── datastore.py
│   │   ├── cache.py
│   │   ├── event_bus.py
│   │   ├── object_store.py
│   │   ├── secrets_provider.py
│   │   ├── token_verifier.py
│   │   └── rate_limiter.py
│   ├── adapters/                 # implementations, grouped by port
│   │   ├── datastore/{memory,postgres}.py
│   │   ├── cache/{memory,redis}.py
│   │   ├── event_bus/{memory,redis}.py
│   │   ├── object_store/{local,s3}.py
│   │   ├── secrets/{env,file,vault}.py
│   │   └── token_verifier/{hs256,jwks}.py
│   ├── api/                      # driving adapters (HTTP)
│   │   ├── deps.py               # FastAPI dependencies (auth, tenant, rate-limit)
│   │   ├── errors.py             # safe, leak-free error handler
│   │   ├── health.py             # /healthz /readyz
│   │   ├── metrics.py            # /metrics (Prometheus/OTel)
│   │   ├── internal.py           # /internal/* (service-to-service token)
│   │   └── v1/                   # /api/v1/* business routes
│   ├── middleware/               # request-id, security headers, timeout, logging
│   ├── security/                 # self-defense
│   │   ├── cwe.json              # bundled MITRE CWE catalog (self-contained copy)
│   │   ├── cwe_guard.py          # pre/post-step CWE self-check gate
│   │   └── step_cwe_map.yaml     # step-type → relevant CWE ids (config, not hardcoded)
│   ├── observability/            # logging + tracing + metrics setup (OTel)
│   ├── manifest.py               # self-describing service descriptor
│   └── schemas/                  # Pydantic request/response models
├── migrations/                   # optional; only if the service persists
├── tests/{unit,integration,security}/
└── ci/                           # license-check, SAST, dep-scan configs
```

---

## 4. The universal ports (Layer 2)

Every port defines an interface + a memory/null adapter + real adapters. Adapter chosen by env
(e.g. `DATASTORE_DRIVER=postgres`). **No default reaches outside the process unless configured.**

| Port | What it abstracts | Adapters (config-selected) |
|---|---|---|
| `DataStore` | persistence / repository | `memory`, `postgres`, (mysql, mongo…) |
| `Cache` | key/value cache | `memory`, `redis` |
| `EventBus` | pub/sub domain events | `memory`, `redis`, (nats, kafka…) |
| `Queue` | async jobs / work | `memory`, `redis`, (sqs, rabbitmq…) |
| `ObjectStore` | blobs / files | `local`, `s3`, (gcs, azure…) |
| `SecretsProvider` | secret loading | `env`, `file`, `vault`, (aws, gcp…) |
| `TokenVerifier` | verify auth tokens | `hs256`, `rs256`, `jwks`, `paseto` |
| `RateLimiter` | throttling | `memory`, `redis` |
| `Telemetry` | logs/metrics/traces | `stdout-json`, `otel` |

> **Security note (beating ammar):** default `TokenVerifier` to **asymmetric** (`rs256`/`jwks`) so a
> service holds only a *public* key and can never forge tokens (ammar used a shared HS256 secret —
> any service could forge). `hs256` stays available for simple single-issuer setups, by config.

---

## 5. Service catalog (Layer 3)

Derived from the SaaS Completeness Checklist. Build in tiers. Each service is self-contained,
cloned from `_template`, and exposes only backend endpoints.

### Tier 0 — Foundation
| Service | Checklist § | Purpose |
|---|---|---|
| `_template` | §3,5,6,10 | The copy-me seed. Everything below starts here. |

### Tier 1 — Core platform (every SaaS needs these)
| Service | § | Key ports → example adapters |
|---|---|---|
| `identity-service` | §1 | `UserStore`, `PasswordHasher`(argon2/bcrypt), `TokenIssuer/Verifier`(jwt/paseto), `MFAProvider`(totp/webauthn/sms), `OAuthProvider`(google/ms/github), `SamlProvider` |
| `tenant-service` | §11 | `TenantStore`, `IsolationStrategy`(shared-row/schema/db), `DomainResolver` |
| `authz-service` | §1 | `PolicyEngine`(rbac/abac/casbin/opa), `PermissionStore` |
| `api-gateway` | §10 | `Router`, `AuthEnforcer`, `RateLimiter`, `Cache` |
| `notification-service` | §9 | `EmailProvider`(smtp/ses/sendgrid), `SmsProvider`(twilio/unifonic), `PushProvider`(fcm/apns), `TemplateEngine`, `PreferenceStore`, `DeliveryTracker` |
| `file-service` | §2 | `ObjectStore`(local/s3/gcs/azure), `Scanner`(av), `ThumbnailGen`, `SignedUrlProvider` |
| `audit-service` | §1,17 | `AuditStore`(append-only), `Exporter` |

### Tier 2 — Growth
| Service | § | Key ports → example adapters |
|---|---|---|
| `billing-service` | §8 | `PaymentGateway`(stripe/moyasar/tap), `PricingEngine`(flat/seat/usage), `InvoiceRenderer`, `TaxCalculator`, `EInvoiceProvider`(zatca) |
| `webhook-service` | §9,10 | `Signer`(hmac), `DeliveryQueue`, `RetryPolicy`, `EndpointStore` |
| `connector-service` | §10 | `Connector` registry → sap/odoo/salesforce/hubspot/xero/quickbooks |
| `search-service` | §2 | `SearchEngine`(pg-fts/opensearch/meilisearch/typesense) |
| `job-service` | §12 | `Queue`(redis/sqs/rabbitmq/kafka), `JobStore`, `DeadLetter` |
| `feature-flag-service` | §17 | `FlagStore`, `Evaluator`(percentage/targeting) |
| `analytics-service` | §15 | `EventSink`(bigquery/snowflake/redshift), `ReportBuilder`, `Scheduler` |
| `compliance-service` | §7,18 | `ConsentStore`, `DsarWorkflow`, `RetentionPolicy`, `LegalHold` |

### Tier 3 — Extended
| Service | § | Key ports → example adapters |
|---|---|---|
| `support-service` | §14 | `TicketStore`, `SlaTimer`, external(zendesk/freshdesk) |
| `backup-service` | §4 | `BackupTarget`(s3/gcs), `Scheduler`, `RestoreRunner` |
| `localization-service` | §16 | `TranslationStore`, provider(lokalise/crowdin) |

> Checklist **§12 Perf, §13 DevOps, §20 Testing** are practices/infra baked into the template + CI,
> not standalone services.

---

## 6. Security model (baked into the template)

Three hard endpoint tiers on every service:

| Path | Auth | Purpose |
|---|---|---|
| `/healthz` `/readyz` `/metrics` | public | probes / scraping |
| `/internal/*` | `X-Internal-Token` (timing-safe compare) | service-to-service |
| `/api/v1/*` | Bearer token via `TokenVerifier` (issuer + audience + pinned alg) | user-facing |

- **Deny-by-default authorization** — capabilities/permissions come from the token claim and are the
  *sole* authority. No claim = no access. A `role` string alone never unlocks admin.
- **Input validation** — Pydantic models on every request; reject unknown/oversized bodies.
- **Safe errors** — never leak messages/stack; generic error codes only.
- **Hardened headers** — CSP, HSTS, `Permissions-Policy`, no-sniff, referrer policy.
- **Redacted logs** — auth headers, passwords, tokens censored.
- **Secrets** — via `SecretsProvider`; never in code or committed files; scanned in CI.
- **Egress** — internal calls support optional mTLS, transparent to callers.

---

## 7. Config-driven everything (how "zero hardcoded" is enforced)

- **One config object** per service (`Pydantic BaseSettings`), validated at boot. Missing/invalid →
  process refuses to start.
- **Adapter selection is config.** `container.py` reads `*_DRIVER` env values and builds the port→adapter
  wiring from a **registry + allowlist** (only known adapters are constructable).
- **Every knob is in `.env.example`**, documented, with safe defaults that reach **no external
  system** (memory/null) until explicitly pointed at real infra.
- **CI guard:** a lint/test step greps for hardcoded hosts/URLs/secrets and fails the build.

Example (illustrative):

```python
# app/container.py — wiring is data, not hardcoded branches everywhere
DATASTORE_ADAPTERS = {"memory": MemoryDataStore, "postgres": PostgresDataStore}

def build_datastore(cfg: Settings) -> DataStore:
    driver = cfg.DATASTORE_DRIVER              # from env, default "memory"
    if driver not in DATASTORE_ADAPTERS:       # allowlist → deny unknown
        raise ConfigError(f"unknown DATASTORE_DRIVER: {driver}")
    return DATASTORE_ADAPTERS[driver](cfg)
```

---

## 8. Self-describing services (the manifest)

Each service ships `app/manifest.py` — a machine-readable descriptor:
`name`, `version`, `capabilities`, `ports used`, `adapters available`, `endpoints`, `events emitted/consumed`.
Exposed at `GET /internal/manifest`. This makes the catalog **discoverable and plug-and-play** — a
gateway or platform can ask a service what it is and what it needs.

---

## 9. Standards & secure development — avoiding CWEs & CVEs

We hold to recognized international standards so the platform is **auditable, certifiable, and safe by
construction** — not "secure because we tried." The goal: known weakness *classes* (CWEs) are designed
out; known *vulnerabilities* (CVEs) never reach production.

### Standards we build to
| Standard | What it gives us | How we apply it |
|---|---|---|
| **ISO/IEC 27001 + 27002** | Information security management (ISMS) controls | Access control, crypto, logging, secrets, change mgmt reflected in the template + docs |
| **ISO/IEC 27701** | Privacy (PIMS) extension | Consent, DSAR, retention → `compliance-service`; data minimization by default |
| **ISO/IEC 27017 / 27018** | Cloud & PII-in-cloud controls | Tenant isolation, encryption, data-residency config knobs |
| **OWASP ASVS** | App security verification levels | Template targets **ASVS L2**; each service tests against ASVS controls |
| **OWASP Top 10** | Common web risks | Mapped to concrete defenses (below) |
| **CWE Top 25** | Most dangerous weakness classes | Each one designed out; CI checks enforce |
| **NIST SSDF (SP 800-218)** | Secure software dev lifecycle | Our CI/CD gates, review, provenance |
| **SLSA** | Supply-chain integrity | SBOM, signed builds, pinned deps |
| **CIS Benchmarks** | Hardened runtime/containers | Distroless/minimal images, non-root, read-only FS |

### How weakness classes (CWEs) are designed out
| Weakness (CWE / OWASP) | Design defense (built into the template) |
|---|---|
| SQL/NoSQL injection (CWE-89) | Parameterized queries only via `DataStore` port; **no string-built SQL**; input validated |
| Broken auth (CWE-287) | `TokenVerifier` with pinned alg + issuer/audience; asymmetric default; MFA in identity-service |
| Broken access control / IDOR (CWE-284, -639) | Deny-by-default authz; tenant scoping enforced at the data layer, not the caller |
| Secrets in code (CWE-798) | `SecretsProvider` port; secret scanning in CI; nothing committed |
| Sensitive data exposure (CWE-200) | Safe generic errors; redacted logs; encryption in transit/at rest via config |
| SSRF (CWE-918) | Egress targets come from a **config allowlist**; callers pass IDs, never URLs |
| XXE / unsafe deserialization (CWE-611, -502) | Safe parsers only; strict Pydantic models; no `pickle`/`eval` on input |
| Path traversal (CWE-22) | `ObjectStore` keys validated against a safe charset; no user-controlled fs paths |
| Weak crypto (CWE-327) | Vetted libs only; argon2/bcrypt for passwords; no home-rolled crypto |
| Missing rate limiting (CWE-770) | `RateLimiter` on auth + API tiers, fail-closed |
| Vulnerable dependencies (CWE-1104 / OWASP A06) | Continuous dep scanning + pinned versions (below) |

### Avoiding CVEs (supply chain)
- **Pinned, hash-locked dependencies** (lockfile) — no floating versions.
- **Continuous vulnerability scanning** in CI (e.g. `pip-audit`, `osv-scanner`, Trivy on images) —
  **build fails on a known CVE** above the agreed severity threshold.
- **SBOM generated per build** (CycloneDX/SPDX) for traceability (SLSA).
- **Minimal attack surface** — fewest dependencies, distroless/slim base images, non-root, read-only FS.
- **Patch SLA** — critical CVEs patched fast; automated dependency-update PRs.
- **License check** (permissive-only, §1.6) runs in the same gate.

### CWE self-check gate (built into every service)

Every service **bundles its own copy of `cwe.json`** — the complete MITRE CWE catalog (Research view
1000: `weaknesses` / `categories` / `views`, DAG parents, bilingual EN/AR). Because services are 100%
self-contained (§1.1), the file is **copied into each service**, never shared at runtime.

A `CweGuard` component uses it to make each service **check itself before and after every step**
(a "step" = a meaningful operation: handle a request, run a query, write a file, call out, emit an event):

- **Pre-step check** — before the operation, verify the defenses for the CWEs *relevant to that step
  type* are in place (e.g. before a DB step → CWE-89 injection guard: params-only, input validated;
  before an egress step → CWE-918 SSRF: target is in the allowlist; before a file step → CWE-22 path
  traversal: key charset-validated). If a precondition fails → **block the step** and log the CWE id.
- **Post-step check** — after the operation, verify no weakness was introduced (e.g. response carries
  no sensitive data → CWE-200; error is generic → CWE-209; no secret in output → CWE-798).
- **Traceable** — every check references a concrete `CWE-###` from the bundled catalog, so logs, audit
  events, and test failures name the exact weakness class. The catalog resolves any CWE id to a
  name + type; a Category/View can never be mis-reported as a Weakness.

The same catalog powers **dev/CI verification** (assert the CWE Top-25 defenses exist) and optional
**runtime guardrails** (fail-closed on a violated precondition). `CweGuard` lives at
`app/security/cwe_guard.py`, catalog at `app/security/cwe.json`, step→CWE mappings in config
(zero-hardcoded — which CWEs apply to which step type is data, not code).

### CI security gate (every service, every PR)
`lint` → `type-check` → `unit/integration/security tests` → **SAST** (e.g. Bandit/Semgrep) →
**dependency CVE scan** → **secret scan** → **license check** → **SBOM** → **container scan**.
Any failure blocks the merge.

---

## 10. Quality gates (definition of done, per service)

A service is "done" only when:

- [ ] Config validates & fails fast; boots with **zero infra** (memory adapters).
- [ ] Every external dependency is behind a port with ≥1 real adapter + a memory/null adapter.
- [ ] **No hardcoded** hosts/URLs/secrets/limits (CI grep passes).
- [ ] Three endpoint tiers wired; auth deny-by-default.
- [ ] Unit + integration + security tests pass.
- [ ] **License check passes** (permissive-only) + SAST + dependency CVE scan + secret scan + SBOM.
- [ ] **CWE Top 25 designed out** and **ASVS L2** controls verified (§9).
- [ ] **`cwe.json` bundled** + `CweGuard` runs pre/post-step self-checks, referencing concrete CWE ids (§9).
- [ ] `/healthz`, `/readyz`, `/metrics`, `/internal/manifest` respond.
- [ ] README documents every env knob and how to clone/customize.

---

## 11. Build order

1. **`_template`** — the seed (this is next).
2. **Tier 1** — identity, tenant, authz, gateway, notification, file, audit.
3. **Tier 2** — billing, webhook, connector, search, job, feature-flag, analytics, compliance.
4. **Tier 3** — support, backup, localization.

Plus a small **`create-service`** scaffolder script: copies `_template`, replaces placeholders,
assigns a port — so a new self-contained service is one command away.

---

## Appendix — the rules in one line each

- Self-contained · Adapter-everything · Zero-hardcoded · Secure-by-default · Fail-fast-config ·
  Permissive-licenses-only · Runs-with-zero-infra · Backend-only · Standards-first (ISO/OWASP/CWE/CVE-clean).

_This document is the source of truth. Update it before changing the architecture._
```
