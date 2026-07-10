# compliance-service

> Self-contained, **general & plug-and-play** privacy/compliance service (SaaS checklist §7):
> **consent** (per-purpose, versioned, cached), **data-subject requests** (access/export/
> erasure/correction) with identity verification and SLA clocks, **legal hold** that blocks
> erasure, **erasure orchestration** across services with completeness tracking, **RoPA**,
> and a **breach register with a 72-hour clock**. KSA-first (**PDPL**), GDPR-compatible.
> Backend only. Zero new runtime deps.

---

## The four rules that make this legally correct

**1. Legal hold beats erasure.** If a subject is under an active hold (litigation, tax,
regulatory investigation), an erasure request is **refused**. Deleting held data is the
violation — keeping it is the lesser harm. *(Tested: held data is never erased.)*

**2. Completion is derived, never declared.** A subject's data lives across services. An
erasure fans out one task per registered **data-map target**, and the request reaches
`completed` **only when every target acknowledges**. A partial delete reported as "done" is
a breach of the subject's rights, so `is_complete()` reads the acks — no one can set a flag.

**3. Identity before PII.** An `access`/`export` request must be **verified** (constant-time
token check) before any personal data is released; otherwise the export endpoint is the best
exfiltration tool an attacker will ever find. `erasure` deliberately needs **no** token —
requiring one would let an attacker *block* someone's erasure.

**4. Consent defaults to DENY.** No record ⇒ not allowed. Consent is **purpose-limited**
(marketing grants nothing for analytics), can **expire**, and a **withdrawal takes effect
immediately** — the cache is invalidated by a per-tenant generation, so a cached "allowed"
can never outlive the withdrawal. Every grant/withdraw appends an **immutable proof**.

## Hot path

`consent_check(subject, purpose)` gates every processing of personal data. It is cached by
`(tenant, subject, purpose, generation)`; any consent change bumps the generation. A repeated
check does **0 datastore reads** after warm (benchmark-asserted); a `grant` writes exactly
the consent record + one proof. A cache outage never grants access.

```
GET /internal/compliance/consent-check?tenant=&subject=&purpose=   → {allowed, reason, cached}
```

## Breach register — the 72-hour clock

Regulators check this. A breach records its **discovery time** and a UTC
`notify_deadline_at = discovered + BREACH_NOTIFY_HOURS` (72). The API exposes
`hours_remaining` and flags `overdue`; notifying late is recorded as `late: true`. A breach
that quietly runs out of clock is the failure mode this exists to prevent.

## Erasure orchestration

| Port | Drivers | Role |
|------|---------|------|
| `Orchestrator` | `null` (records, dispatches nothing — and **says so**) · `eventbus` · `job-http` (durable jobs in job-service) | one task per data-map target |
| `Clock` | `system` · `manual` | SLA/breach math, injected for deterministic tests |

`job-http` is the production choice: job-service retries, dead-letters and never loses an
erasure task, and the job's `unique_key` makes a re-dispatch idempotent. `null` returns
`False` from `dispatch` — it never claims work was sent.

## What's recorded

- **Consent** + append-only **ConsentProof** (notice version, method, actor, IP, time).
- **DSAR** — type, status, verification, per-target acks, **UTC deadline** + `overdue`.
- **Legal holds**, **data map** (service × category), **RoPA** (purpose, **lawful basis**,
  categories, retention, **residency**, cross-border, recipients), **breaches**.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `GET /internal/compliance/consent-check` | **hot path** — may we process this? |
| internal | `POST /internal/compliance/dsar/{id}/target-ack` | a service confirms its slice is erased |
| user | `POST /consent/grant` · `/consent/withdraw` · `GET /consent/history` | consent + proof |
| user | `POST /dsar` · `/{id}/verify` · `/{id}/process` · `GET /dsar` | data-subject requests |
| user | `POST /legal-holds` · `/{id}/release` | legal hold |
| user | `POST/GET /data-targets` · `/ropa` · `/breaches` · `/breaches/{id}/notify` | data map, RoPA, breaches |

Caps: `platform.compliance` (cross-tenant) · `compliance.manage` · `compliance.read`.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope
Privacy-notice/DPA document generation · SDAIA/regulator API submission · cookie-banner UI
(the backend records the consent) · vendor-questionnaire workflow. Retention *enforcement*
lives with each service (see audit-service `enforce-retention`), driven by job-service cron.
