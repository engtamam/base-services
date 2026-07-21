# Phase 13 — zatca-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** KSA e-invoicing service (SaaS checklist
> §7 ZATCA compliance + §8 Fatoora integration). It owns the **smart orchestration**
> — generic UBL 2.1 build, the **PIH/ICV tamper-evident chain**, the transient-vs-hard
> **error classification**, retry with backoff, fail-closed/non-fatal issuance — and
> **delegates the hard crypto** (XAdES sign, TLV QR, hash, CSR) to the **official ZATCA
> SDK behind a `ZatcaSigner` port**. Fatoora submission and QR rendering are ports too.
> Config-driven, multi-tenant, zero new runtime deps.
>
> _Reference (studied): a second reference implementation `channels/cloud_api/zatca_*.py` + Java `services/zatca-service` (official SDK); the reference implementation `billing-service/.../zatca-*`. Memory: [[zatca-service]], [[ksa-market]]._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · backend-only._

---

## 0. Definition of done (Phase 13)

- [ ] `services/zatca-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Delegated crypto:** `ZatcaSigner` port (`sdk-sidecar` calls the official-SDK Java service · `null` fails closed) — csr / sign / validate. We **never** reimplement XAdES/TLV/hash.
- [ ] **Pluggable submission:** `FatooraClient` port (`sidecar` · `direct` · `null`) — report (B2C) / clear (B2B) / onboard. Endpoints + CSID from **config/vault**, never hardcoded.
- [ ] **Pluggable QR render:** `QrRenderer` port (`null` · `segno` optional) — renders the signer's TLV to PNG/SVG (PDF stays in billing).
- [ ] **Generic invoice model** — any seller/buyer/VAT/currency/lines; type auto (standard if buyer has VAT → clearance, else simplified → reporting).
- [ ] **PIH/ICV chain** — genesis PIH, `next_pih = this invoice hash`, monotonic ICV, `verify_chain` (first-break-wins: genesis/pih/icv-gap/unsigned). Reuses the audit-service chain idea.
- [ ] **Smart state machine** — `none→pending→signed→submitted→cleared/reported/rejected` via `classify()` (200 ok · 202 warning-ok · 400/401/413 rejected-no-retry · 429/5xx/timeout pending-retry).
- [ ] **Anti-miss:** fail-closed signing (never fabricate) · non-fatal issuance (never blocks caller) · **no ICV consumed on failure** · retry **sweep** (backoff `base·2ⁿ`, cap → needs-attention, idempotent) · manual resubmit.
- [ ] **Security:** CSID sealed at rest (AES-GCM) · **secret-free logs** (CWE-532) · Fatoora egress SSRF-guarded (config allowlist) · admin deny-by-default + capability-gated · tenant-scoped.
- [ ] **Zero new runtime deps · zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot proven (finalize fail-closed → pending; mock signer → cleared; chain verify; tamper detected).

---

## 1. Prerequisites & decisions

1. **Zero new runtime deps.** UBL/chain/classify/backoff are stdlib; sidecar/Fatoora HTTP via stdlib `urllib` (like notification's `notify_http`); CSID sealing via `cryptography` (already present through `pyjwt[crypto]`). QR rendering is an **optional `[qr]` extra** (`segno`, MIT) — `null` by default → reuse the template venv.
2. **We wrap the SDK, never reimplement it.** The `sdk-sidecar` signer POSTs to the official-SDK Java service (`/internal/zatca/{csr,sign,validate}`). `null` signer **fails closed** (keep Phase-1, no fabricated signature). This is the whole point — crypto must be byte-exact.
3. **PDF is billing's job.** zatca returns signed XML + QR (TLV/image); billing composes the EN/AR PDF.
4. **Multi-tenant.** Per-tenant settings + CSID (sealed); each tenant onboards its own seller identity/environment. Reuses the `DataStore`.

---

## 2. Steps

### Step 13.1 — Generate skeleton
- `python scripts/create_service.py zatca-service --desc "KSA ZATCA Fatoora e-invoicing"`.
- Optional `[qr]` extra (`segno`) in pyproject; default renderer `null` → reuse template venv. Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 13.2 — Config (zero-hardcoded)
- `ZATCA_SIGNER_DRIVER` (null), `ZATCA_SIGNER_URL`, `ZATCA_SIGNER_TOKEN`; `FATOORA_CLIENT_DRIVER` (null), `FATOORA_ENV` (sandbox|simulation|production), `FATOORA_BASE_URL` (per-env, config), `QR_RENDERER_DRIVER` (null); `VAT_RATE` (0.15), `CURRENCY` (SAR); retry `ZATCA_RETRY_BASE_MS`/`_MAX_MS`/`_MAX_ATTEMPTS`; `ZATCA_SEAL_KEY` (falls back to INTERNAL token); `ZATCA_LIST_LIMIT`. Update `.env.example`.
- **Check:** boots with null drivers (fail-closed); fail-fast on bad values.

### Step 13.3 — Ports
- `app/ports/zatca_signer.py` (`generate_csr`, `sign`, `validate`), `app/ports/fatoora_client.py` (`report`, `clear`, `onboard_compliance`, `onboard_production`), `app/ports/qr_renderer.py` (`render(tlv_base64) -> (content_type, bytes) | None`).
- **Check:** import pulls no third-party driver.

### Step 13.4 — Adapters + registry
- Signer: `null.py` (raise `SignerUnavailable` — fail closed), `sidecar.py` (stdlib HTTP to the SDK service, X-Internal-Token, secret-free). Fatoora: `null.py`, `sidecar.py` (via signer sidecar `/report`,`/clear`), `direct.py` (Fatoora REST via urllib, Basic-auth CSID, env base URL). QR: `null.py`, `segno_renderer.py` (lazy `[qr]`).
- Extend `container.py` with `zatca_signer`, `fatoora_client`, `qr_renderer` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; null signer fails closed; sidecar builds request correctly (mock transport).

### Step 13.5 — Core pure functions
- `core/ubl.py` — build **unsigned** UBL 2.1 from the generic invoice (owns UBL body + PIH/ICV; omits signature/QR — the signer adds those). `core/chain.py` — `GENESIS_PIH`, `verify_chain(links)` first-break-wins. `core/classify.py` — Fatoora outcome (`cleared/reported/warning/rejected/transient`) + warnings/errors. `core/backoff.py` — `compute_retry_schedule(attempt, base, max, cap)`.
- **Check:** UBL well-formed + correct type/profile; chain detects genesis/pih/icv-gap/unsigned; classify covers the HTTP table; backoff escalates + caps.

### Step 13.6 — Domain: models + stores
- `core/models.py` `InvoiceIn` (seller, buyer, lines, currency, vat_rate, number, issued_at) + `ZatcaInvoice` (id, tenant, number, uuid, icv, pih, hash, status, signed_xml, qr, cleared_xml, invoice_type, attempts, next_retry_at, warnings, errors, message).
- `core/stores.py` `InvoiceStore` (create/get/list/by-status/needs-attention; **allocate_slot** = atomic ICV+PIH) + `SettingsStore` (per-tenant seller/env + **sealed CSID** via AES-GCM). `core/seal.py` (AES-GCM seal/unseal).
- **Check:** ICV monotonic + PIH linkage; idempotent re-issue; CSID sealed (never returned in views).

### Step 13.7 — Core ZatcaService
- `core/zatca_service.py`: `finalize(invoice, tenant)` → allocate slot → build UBL → **sign** (fail-closed) → **submit** (clear if buyer VAT else report) → persist status + `next_pih` (+ warnings/errors) → **no ICV consumed on failure**, never raises to caller; `sweep()` (retry due, backoff, cap, idempotent skip-accepted); `resubmit(id)`; `verify_chain(tenant)`; onboarding (`onboard_compliance`, `onboard_production`).
- **Check:** unit — fail-closed→pending, cleared/reported/rejected classification, sweep retries + caps, resubmit resets, chain verify, tenant isolation.

### Step 13.8 — Routes + manifest
- Internal: `POST /internal/zatca/finalize` (caller e-invoices), `POST /internal/zatca/sweep`.
- Admin (`platform.zatca` cross-tenant / `zatca.manage` own): `GET /api/v1/zatca/invoices` (needs-attention filter), `POST /api/v1/zatca/invoices/{id}/resubmit`, `POST /api/v1/zatca/sweep`, `GET /api/v1/zatca/chain/verify`, `GET/PUT /api/v1/zatca/settings`, `POST /api/v1/zatca/onboard/{compliance,production}`, `GET /api/v1/zatca/invoices/{id}/qr`.
- Wire `rate_limit`, deny-by-default, `guard.step("respond")`, `guard.step("egress")` around Fatoora/sidecar calls (SSRF). Manifest caps + endpoints + ports.
- **Check:** cross-tenant needs platform cap; PDP/finalize behind token; settings never leak CSID.

### Step 13.9 — Tests
- Unit: UBL build (standard vs simplified, VAT sum), chain first-break matrix, classify all HTTP cases, backoff escalation+cap, finalize fail-closed (no ICV consumed), sweep idempotent + capped, resubmit, seal roundtrip, tenant scoping.
- Integration: finalize with `null` signer → pending; with a **mock signer + mock fatoora** → cleared/reported; chain verify ok → tamper → detected; settings GET/PUT (no CSID leak); onboarding flow (mock); admin cap-gating.
- Security: secret-free (no keys/CSID in responses/logs), Fatoora egress allowlisted, deny-by-default, cross-tenant isolation.
- **Check:** all green.

### Step 13.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: `finalize` a B2C invoice with `null` signer → `pending` (fail-closed, ICV not consumed); swap in a **mock signer/fatoora** → `reported`, chain verifies; mutate a stored hash → chain-verify reports the break.
- **Check:** end-to-end orchestration correct.

### Step 13.11 — Docs
- Service `README.md`: architecture (delegate crypto), the ports, the lifecycle/state machine, chain, onboarding, config, security, "PDF is billing's job".
- Update `docs/architecture-plan.md` (zatca ✅ — Tier-2 KSA) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **The official-SDK Java sidecar itself** — a separate deployable we call (not bundled → base-services stays permissive). Reuse the reference image or build from the studied Java.
- **PDF (EN/AR) rendering** → billing-service.
- **Phase-1 PDF/A-3** archival format → billing/export.
- **Live Fatoora onboarding** (real OTP/CSID) → operational; code path is exercised against mocks.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #131 | 13.1 Skeleton | ✅ completed |
| #132 | 13.2 Config | ✅ completed |
| #133 | 13.3 Ports | ✅ completed |
| #134 | 13.4 Adapters + registry | ✅ completed |
| #135 | 13.5 Core pure functions | ✅ completed |
| #136 | 13.6 Domain models + stores | ✅ completed |
| #137 | 13.7 Core ZatcaService | ✅ completed |
| #138 | 13.8 Routes + manifest | ✅ completed |
| #139 | 13.9 Tests | ✅ completed |
| #140 | 13.10 Verify + live boot | ✅ completed |
| #141 | 13.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (140 tests; gate PASS; full lifecycle live) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is general + smart (the point)

| Choice | Knob | Options |
|---|---|---|
| Crypto | `ZATCA_SIGNER_DRIVER` | null(fail-closed) · sdk-sidecar |
| Submission | `FATOORA_CLIENT_DRIVER` | null · sidecar · direct |
| QR render | `QR_RENDERER_DRIVER` | null · segno |
| Environment | `FATOORA_ENV` / `FATOORA_BASE_URL` | sandbox · simulation · production |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Retry | `ZATCA_RETRY_*` | base/max/attempts |

Same battle-tested correctness (wrap the official SDK), but **swappable, config-driven,
multi-tenant, chain-verified, event-ready** — the plug-and-play version any Saudi SaaS drops in.

_When every box is ✅, zatca-service is live — Tier-2 KSA e-invoicing._
