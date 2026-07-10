# Phase 18 — webhook-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** webhook service (SaaS checklist §10
> *Webhook System* + *Integration Monitoring*): **outbound** subscriptions with signed,
> retried, circuit-broken, dead-lettered delivery; **inbound** signed ingest with replay
> protection; per-endpoint health/monitoring. Backend only.
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Is this on the hot path? → YES, twice. Cache it and prove it.**
- **Inbound ingest** `POST /inbound/{source}` receives every third-party event (Stripe, GitHub…). It must: verify → dedupe → persist → enqueue → `200`. **No synchronous processing, no scans.**
- **`dispatch(event)`** runs for every internal event and must find the endpoints subscribed to `(tenant, event_type)`. Naive = full scan of all endpoints **per event**.
- [ ] Cache endpoint sets keyed `subs:<tenant>:<event_type>`, TTL `SUBSCRIPTION_CACHE_TTL_SECONDS`, invalidated on any endpoint write.
- [ ] **Benchmark asserts**: after warm, `dispatch()` does **0 datastore reads and 0 writes** for endpoint lookup; ingest does **exactly 1 write** (the dedupe/event row) and **0 scans**.

**(2) Does every config knob have a consumer? → guard it.**
- [ ] Port the `test_every_settings_field_is_consumed_somewhere` guard; it fails on any unwired `Settings` field.

**(3) What does production need that the checklist omits? → build it.**
The checklist says only "signature verification / retry and dead-letter". Production also needs:
- [ ] **SSRF guard on outbound URLs** ⚠ — customers register arbitrary URLs and *we* make the request. Block private, loopback, link-local, multicast, reserved ranges and **cloud metadata (169.254.169.254, fd00:ec2::254)**; require `https` (configurable); **resolve DNS then pin the connection to that validated IP** (defeats DNS rebinding); optional host allow/deny lists. *(CWE-918)*
- [ ] **Replay protection on inbound** — signature covers a **timestamp**; reject outside `INBOUND_TIMESTAMP_TOLERANCE_SECONDS`; **dedupe on the provider's event id**.
- [ ] **Backoff with jitter** — plain `base·2ⁿ` synchronises every failed delivery into a retry storm. Full-jitter required.
- [ ] **Circuit breaker per endpoint** — after `CIRCUIT_FAILURE_THRESHOLD` consecutive failures, open the circuit (stop delivering, keep queueing); half-open probe after `CIRCUIT_RESET_SECONDS`. A dead customer endpoint must never consume the worker pool.
- [ ] **Dead-letter + replay** — a DLQ nobody can replay is a landfill. `POST /deliveries/{id}/replay`, `POST /events/{id}/replay`.
- [ ] **Secret rotation** — endpoints hold `secrets[]`; sign with the primary, accept both during rotation.
- [ ] **Per-endpoint concurrency cap** + **payload/response size caps** — never DDoS a customer, never read a 1 GB response body.
- [ ] **Delivery log** — status, response code, truncated body, **latency_ms**, attempt — feeds Integration Monitoring (health, error rate).
- [ ] **Endpoint health + auto-disable** — success rate, last error, circuit state; `GET /endpoints/{id}/health`.

**(4) What's the correctness footgun here?**
- [ ] **SSRF / DNS rebinding** — validate *and pin* the resolved IP; re-validate on every attempt (DNS may change between retries).
- [ ] **At-least-once ⇒ consumers must dedupe.** The `event_id` must be **stable across every retry** and sent as `Idempotency-Key`. Never regenerate it per attempt.
- [ ] **Sign the exact bytes sent** (serialize once, sign that buffer, send that buffer), timestamp included, compared **constant-time**.
- [ ] **Inbound dedupe on the provider's event id**, never a body hash — two distinct events can carry identical bodies.
- [ ] **Retries never drop.** Exhausted attempts → DLQ, not discard. Circuit-open → stay queued, not dropped.
- [ ] **Non-retryable vs retryable**: 2xx ok; 4xx terminal **except** 408/429; 5xx/timeout/DNS retryable. A blind retry on 400 hammers a customer forever.

### Functional (checklist §10)
- [ ] Outbound: register/update/delete endpoints, event-type subscriptions, signed delivery, retry, DLQ.
- [ ] Inbound: signed ingest per source, verify, dedupe, persist, publish to EventBus.
- [ ] Monitoring: endpoint health, error rate, delivery log, DLQ size.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; hot-path benchmark + config-knob guard pass; live boot proves each item. **One pass — no v2.**

---

## 1. Ports

| Port | Drivers | Role |
|------|---------|------|
| `WebhookTransport` | `stdlib` · `httpx` · `recording` (no network) · `null` | make the outbound request (SSRF-pinned) |
| `Signer` | `hmac-sha256` (Stripe-style `t=…,v1=…`) · `null` | sign outbound / verify inbound |

Reuses `DataStore`, `Cache` (hot path), `Queue` (delivery jobs), `EventBus` (subscribe internal events → dispatch), `Telemetry`.

## 2. Steps

**STATUS: ✅ ALL DONE — 232 tests, gate PASS, live verified. The config-knob guard caught 1 unwired knob (INBOUND_DEDUPE_TTL_SECONDS) mid-build; now wired.**

| Task | Step |
|---|---|
| 18.1 | Skeleton |
| 18.2 | Config (every knob consumed) + `.env.example` |
| 18.3 | Ports (`WebhookTransport`, `Signer`) |
| 18.4 | **`core/ssrf.py`** — URL validation + DNS resolve & **IP pinning** (CWE-918) |
| 18.5 | **`core/signing.py`** (timestamp + constant-time) · **`core/backoff.py`** (full jitter) · **`core/circuit.py`** |
| 18.6 | Adapters + registry (transport: stdlib/httpx/recording/null; signer: hmac/null) |
| 18.7 | Models + stores (Endpoint, Event, Delivery, InboundEvent) + **cached subscription index** |
| 18.8 | Core `WebhookService` — dispatch, deliver, classify, retry, circuit, DLQ, replay, rotation |
| 18.9 | Core `InboundService` — verify, timestamp tolerance, dedupe, publish |
| 18.10 | Routes + manifest (outbound admin, inbound ingest, health/monitoring, replay) |
| 18.11 | Tests — incl. **hot-path benchmark**, **config-knob guard**, SSRF matrix, jitter, circuit, DLQ, replay, rotation |
| 18.12 | Verify + live boot + docs + memory |

## 3. Out of scope
Third-party connector catalogue (ERP/CRM — that's connector-service) · developer portal/OpenAPI sandbox (UI) · API-key issuance (identity) · gateway routing (api-gateway).
