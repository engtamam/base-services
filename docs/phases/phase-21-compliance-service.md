# Phase 21 — compliance-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** privacy/compliance service (SaaS checklist
> §7 — PDPL + GDPR): **consent** management (per-purpose, versioned, cached), **data-subject
> requests** (access/export, erasure, correction) with identity verification + SLA clocks,
> **legal hold** that blocks erasure, **erasure orchestration** across services with
> completeness tracking, **records of processing (RoPA)**, retention policies, and a
> **breach register with a 72-hour clock**. Self-contained; erasure/export fan-out via a
> pluggable `Orchestrator` port (events / job-service). KSA-first (PDPL), GDPR-compatible.
>
> _Planned **past** the checklist per memory `phase-doc-rules` — the four questions are answered in §0 before any code._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only. No v2 — one pass, full._

---

## 0. Definition of done

### The four questions (answered before the plan)

**(1) Hot path? → YES: `consent_check`. Cache it, prove zero reads, invalidate on change.**
`consent_check(subject, purpose)` gates every processing of personal data.
- [ ] Cached by `(tenant, subject, purpose, generation)`; generation bumped on any consent grant/withdraw → **a withdrawal takes effect immediately**.
- [ ] **Benchmark**: a repeated check does **0 datastore reads** after warm; a consent change busts it.
- [ ] **Default DENY** — no consent record ⇒ not allowed (never allow-by-default).

**(2) Every config knob has a consumer → guard test from the start.**

**(3) What production needs that the checklist omits → build it.**
- [ ] **Data-map + erasure completeness** — a subject's data lives across services; an erasure fans out a task per registered target and is `completed` only when **every** target confirms. A partial delete is a violation, not success.
- [ ] **Legal hold blocks erasure** — while a subject is on hold, erasure is refused (hold wins).
- [ ] **Consent versioning + immutable proof** — which privacy-notice version was accepted, when, by what method; an append-only proof record for audits.
- [ ] **DSAR identity verification** — an access/export request must be verified (token) before any PII is released; unverified → refused.
- [ ] **SLA deadline clocks** — DSAR due in `DSAR_SLA_DAYS` (PDPL/GDPR ~30); breach notification due in `BREACH_NOTIFY_HOURS` (72). Computed in **UTC**; overdue is flagged.
- [ ] **Breach register** — record a breach, its clock, affected subjects/categories, and notification status.
- [ ] **RoPA** — processing activities: purpose, **lawful basis**, data categories, retention, **residency** + cross-border flag.
- [ ] **Retention policies** per data category (a scheduler enforces via job-service, like audit).
- [ ] **Purpose limitation** — consent is per-purpose; a check for purpose X ignores consent for purpose Y.

**(4) Correctness footguns.**
- [ ] **Legal hold vs erasure** → erasure checks holds first and refuses; a test proves held data is never erased.
- [ ] **"Erased" while data remains** → status derives from per-target completion, not a flag someone sets.
- [ ] **Consent default** → deny on unknown/expired; withdrawal reflected immediately (cache bust).
- [ ] **DSAR to wrong identity** → verification required before export.
- [ ] **Deadline timezone** → UTC everywhere.
- [ ] **Double-processing** → erasure/DSAR idempotent (a re-submit returns the existing request).

### Functional (checklist §7)
- [ ] Consent grant/withdraw/check/history; DSAR access/export/erasure/correction lifecycle; legal hold place/release; RoPA CRUD; retention policies; breach register; data-map registration.

### Gates
- [ ] `pytest` + `ci/security_gate.py` green; **hot-path benchmark + config-knob guard** pass; live boot proves each item. **One pass, no v2.**

---

## 1. Ports

| Port | Drivers | Role |
|------|---------|------|
| `Orchestrator` | `null` (record only) · `eventbus` (publish erasure/export tasks) · `job-http` (enqueue in job-service) | fan out per-target erasure/export work |
| `Clock` | `system` · `manual` | deadline/SLA math — injected for deterministic tests |

Reuses `DataStore`, `Cache` (consent hot path), `EventBus`, `Telemetry`.

## 2. Steps

**STATUS: ✅ ALL DONE — 157 tests, gate PASS, live verified (default-deny, hold-blocks-erasure, completeness from acks, 72h breach clock).**

| Task | Step |
|---|---|
| 21.1 | Skeleton |
| 21.2 | Config (every knob consumed) + `.env.example` |
| 21.3 | Ports (`Orchestrator`, `Clock`) + adapters + registry |
| 21.4 | Models + stores (Consent, ConsentProof, DSAR, LegalHold, ProcessingActivity, Breach, DataTarget) + per-tenant generation |
| 21.5 | Core `ConsentService` — grant/withdraw/**check (cached, default-deny, purpose-limited)**/history + proof |
| 21.6 | Core `DsarService` — create/verify/process access·export·erasure·correction; **legal-hold block**; erasure fan-out + **completeness**; SLA deadline |
| 21.7 | Core `ComplianceService` — legal hold, RoPA, retention policies, **breach register (72h clock)**, data-map |
| 21.8 | Routes + manifest (internal consent-check + target-ack; admin DSAR/holds/RoPA/breach/retention) |
| 21.9 | Tests — hot-path benchmark, config-knob guard, default-deny, hold-blocks-erasure, erasure completeness, DSAR verification, SLA/breach clock (UTC), idempotency, purpose-limitation |
| 21.10 | Verify + live boot + docs + memory |

## 3. Out of scope
Automated privacy-notice/DPA document generation · SDAIA/regulator API submission · cookie-banner UI (backend records the consent) · vendor-questionnaire workflow. The service records/orchestrates; UIs and regulator filings are separate.
