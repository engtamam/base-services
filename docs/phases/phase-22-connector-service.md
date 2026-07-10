# Phase 22 — connector-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** third-party integration service (SaaS
> checklist §10 *Third-Party Integrations* + *Integration Monitoring*): manage connections
> (OAuth2/API-key), **encrypted credentials at rest**, **single-flight token refresh**,
> incremental **sync with a safe watermark**, structured field mapping, per-record
> idempotency + partial-failure results, `Retry-After`-aware rate limiting, circuit
> breaker, SSRF-guarded self-hosted URLs, and per-connection health.
>
> The headline design: a **spec-driven generic OAuth2+REST connector**, so most providers
> are *config, not code*. Concrete verified adapters (Slack, HubSpot) prove the shape.
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only. No v2 — one pass, full._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Hot path? → YES: credential/token resolution. Cache it, prove it, and prove single-flight.**
Every outbound provider call resolves a connection's access token first.
- [ ] Cache token + connection config, keyed by a per-tenant **generation** bumped on connect/update/disconnect → **revocation is immediate**.
- [ ] **Benchmark**: repeated `credentials(conn_id)` does **0 datastore reads** after warm.
- [ ] **Benchmark**: N concurrent calls on an expiring token trigger **exactly one** refresh (single-flight), not N.

**(2) Every config knob has a consumer → guard test from the start.**

**(3) What production needs that the checklist omits → build it.**
- [ ] **Encrypted credentials at rest** (AES-256-GCM); never returned by any API; redacted from logs/errors.
- [ ] **Single-flight refresh** + **early refresh** (`TOKEN_REFRESH_SKEW_SECONDS`) — providers *rotate* refresh tokens, so two concurrent refreshes can permanently brick a connection.
- [ ] **Incremental sync with a watermark advanced ONLY after the batch is durably processed.**
- [ ] **Per-record idempotency** + **partial-failure results** — 997 ok / 3 failed ⇒ record both, dead-letter the 3; never "success", never a full rollback.
- [ ] **`Retry-After`-aware backoff** + per-connection **circuit breaker** (reuse the webhook pattern).
- [ ] **SSRF guard** on self-hosted instance URLs (Odoo/SAP on-prem) — validate + pin, block private/metadata ranges.
- [ ] **Echo-loop prevention** on bidirectional sync (an `origin` marker; we never re-push what we just pulled).
- [ ] **Structured field mapping** (declarative rename/const/path — **no eval**).
- [ ] **Sync-run history**: records in/out, failures, duration, error rate → integration monitoring.
- [ ] **Scope validation** at connect time; **disconnect revokes** (provider call + cache bust).

**(4) Correctness footguns.**
- [ ] **Refresh-token rotation race** → single-flight; the rotated refresh token is stored atomically before use.
- [ ] **Watermark before commit** → permanent silent data loss. Advance after.
- [ ] **Double-write on retry** → per-record `idempotency_key` (stable, derived from source id + run).
- [ ] **Bidirectional echo loop** → origin marker breaks A→B→A.
- [ ] **SSRF** via user-supplied instance URL.
- [ ] **Credential leakage** in logs, errors, or API responses.
- [ ] **Clock skew** on token expiry → refresh early, never trust exact expiry.

### Functional (checklist §10)
- [ ] Connect (OAuth2 code exchange / API key), list/get/disconnect connections; define a provider spec; run a sync (pull/push); sync history; per-connection health + error rate.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; **hot-path + single-flight benchmarks**, **config-knob guard** pass; live boot proves each item. **One pass, no v2.**

---

## 1. Ports

| Port | Drivers | Role |
|------|---------|------|
| `ConnectorProvider` | `oauth2-rest` (**spec-driven, generic**) · `slack` · `hubspot` · `recording` (no network) · `null` | exchange/refresh tokens, fetch, push, revoke |
| `Clock` | `system` · `manual` | token expiry / backoff, injected for tests |

Reuses `DataStore`, `Cache` (hot path), `EventBus`, `Telemetry`. `core/seal.py` (AES-GCM) for credentials; `core/ssrf.py` for instance URLs.

## 2. Steps

**STATUS: ✅ ALL DONE — 186 tests, gate PASS, live verified. Guards caught: cache serving an EXPIRED token (defeating skew/refresh), per-instance lock breaking single-flight, and 2 unwired knobs (HTTP_TIMEOUT_SECONDS / MAX_RESPONSE_BYTES).**

| Task | Step |
|---|---|
| 22.1 | Skeleton |
| 22.2 | Config (every knob consumed) + `.env.example` |
| 22.3 | `core/seal.py` (AES-GCM) · `core/ssrf.py` (instance URL guard) · `core/backoff.py` (Retry-After aware) — pure |
| 22.4 | `core/mapping.py` (structured field map, no eval) · `core/circuit.py` — pure |
| 22.5 | Ports + adapters (oauth2-rest spec-driven, slack, hubspot verified, recording, null) + registry |
| 22.6 | Models + stores (Connection, ProviderSpec, SyncRun, SyncRecord/DLQ) + generation |
| 22.7 | Core `TokenManager` — cached credentials, **single-flight refresh**, early-refresh skew, rotated-token atomic store |
| 22.8 | Core `SyncEngine` — pull/push, watermark-after-commit, per-record idempotency, partial results + DLQ, echo-loop guard, circuit breaker |
| 22.9 | Routes + manifest (connect/callback/disconnect, specs, sync run/history, health) |
| 22.10 | Tests — hot-path + single-flight benchmarks, knob guard, SSRF matrix, watermark safety, partial failure, echo loop, secret redaction, rotation race |
| 22.11 | Verify + live boot + docs + memory |

## 3. Out of scope
Per-vendor deep object models (SAP IDoc, Salesforce SOQL) — the spec-driven connector covers REST/OAuth2; a vendor needing a bespoke protocol gets its own adapter behind the same port · GUI mapping builder · vendor-risk questionnaire (compliance-service).
