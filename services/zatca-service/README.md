# zatca-service

> Self-contained, **general & plug-and-play** KSA e-invoicing service (SaaS checklist
> §7 ZATCA + §8 Fatoora). It owns the **smart orchestration** — generic UBL 2.1 build,
> the **PIH/ICV tamper-evident chain**, transient-vs-hard **error classification**,
> retry with backoff, fail-closed/non-fatal issuance — and **delegates the hard crypto**
> (XAdES sign, TLV QR, hash, CSR) to the **official ZATCA SDK behind a `ZatcaSigner`
> port**. Multi-tenant, config-driven, **zero new runtime deps**. Backend only.

---

## Why this design

ZATCA crypto (XAdES canonicalization, invoice hash, TLV QR with DER long-form length,
secp256k1 CSR) must be **byte-exact** or Fatoora rejects it — so we **never reimplement
it**. The `sdk-sidecar` signer calls the official-SDK service; the `null` signer **fails
closed** (keep Phase-1, never fabricate a signature). Everything else — the part that's
actually *product* — is ours and swappable.

## Ports (config-selected)

| Port | Drivers | Role |
|------|---------|------|
| `ZatcaSigner` | `null` · `sdk-sidecar` | csr / sign / validate (delegated crypto) |
| `FatooraClient` | `null` · `sidecar` · `direct` | report (B2C) / clear (B2B) / onboard |
| `QrRenderer` | `null` · `segno` (`[qr]` extra) | render the TLV → PNG (PDF is billing's job) |

## Lifecycle & state machine

```
finalize → allocate ICV+PIH slot → build UBL → SIGN (fail-closed) → SUBMIT
   simplified (B2C, no buyer VAT) → REPORT (≤24h)
   standard  (B2B, buyer VAT)     → CLEAR  (real-time)
status: none → pending → signed → submitted → cleared | reported | rejected
```
`classify()` maps the Fatoora answer: **200** ok · **202** accepted-with-warnings ·
**400/413** rejected (terminal — fix, don't blind-retry) · **401/429/5xx/timeout**
transient → `pending` for the **retry sweep** (backoff `base·2ⁿ`, cap → needs-attention,
idempotent — never double-submits).

## Anti-miss guarantees

- **Fail-closed signing** — an unavailable signer never fabricates a signature.
- **Non-fatal issuance** — `finalize` never raises to the caller.
- **No ICV wasted** — the slot is allocated once (idempotent by invoice number) and
  reused on every retry, so the **PIH/ICV chain never gaps**.
- **Tamper-evident chain** — `GET /chain/verify` walks PIH/ICV from genesis, first-break-
  wins (`genesis_mismatch`/`pih_mismatch`/`icv_gap`/`unsigned_link`).

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/zatca/finalize` | e-invoice one invoice (any caller) |
| internal | `POST /internal/zatca/sweep` | run the retry sweep |
| user | `GET/PUT /api/v1/zatca/settings` | seller/env (CSID **never** returned) |
| user | `POST /api/v1/zatca/onboard/compliance` | CSR → compliance CSID |
| user | `GET /api/v1/zatca/invoices?needs_attention=` | list / needs-attention |
| user | `POST /api/v1/zatca/invoices/{id}/resubmit` | manual resubmit |
| user | `GET /api/v1/zatca/invoices/{id}/qr` | rendered QR (if a renderer is set) |
| user | `GET /api/v1/zatca/chain/verify` | chain integrity |
| user | `POST /api/v1/zatca/sweep` | run sweep on demand |

Admin is `platform.zatca` (cross-tenant) or `zatca.manage` (own tenant, forced scope).

## Security

CSID sealed at rest (**AES-256-GCM**); **secret-free logs** (keys/bodies never logged,
CWE-532); Fatoora egress host is config (**SSRF-guarded** allowlist); admin deny-by-default
+ capability-gated; per-tenant isolation. The official SDK is a **separate sidecar** we
call over HTTP — not bundled — so base-services stays permissive-licensed.

## Generic invoice input

```json
POST /internal/zatca/finalize
{ "tenant": "acme",
  "invoice": { "number": "INV-1", "issued_at": 1700000000,
    "currency": "SAR", "vat_rate": "0.15",
    "seller": { "name": "ACME", "vat_number": "3000...", "address": "Riyadh" },
    "buyer":  { "name": "Buyer", "vat_number": "3111..." },
    "lines":  [ { "description": "Plan", "qty": 1, "amount_minor": 10000 } ] } }
```
Omit the buyer `vat_number` → B2C/simplified (report); include it → B2B/standard (clear).
Any seller/buyer/currency/VAT/lines — **not** tied to one platform's record shape.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci,qr]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope

The official-SDK **Java sidecar** (a separate deployable) · **PDF (EN/AR)** → billing ·
live Fatoora onboarding (operational: real OTP/CSID). The code path is exercised against
mock signer/Fatoora transports.

## Rate limiting (every request, config-driven)

Sliding-window (GCRA) limiter — **no boundary burst**, and **atomic** (a check-and-increment race
cannot exceed the limit). Every response carries `RateLimit-Limit/Remaining/Reset`; a 429 also
carries `Retry-After`. On a Redis outage it **degrades to a per-pod limiter** (coarser, still
limiting) rather than failing open or 500-ing.

| Choice | Env knob | Options / example |
|--------|----------|-------------------|
| Backend | `RATELIMITER_DRIVER` | `memory`(per-pod) · `redis`(shared) |
| Budget | `RATELIMIT_MAX` / `RATELIMIT_WINDOW_SECONDS` | e.g. `100` / `60` |
| Key | `RATELIMIT_KEY_PARTS` | `subject,ip` · add `tenant`/`route` |
| Per-route budgets | `RATELIMIT_ROUTES` | `[{"prefix","class","limit","window_s","cost"}]` |
| Only count failures | `RATELIMIT_COUNT_STATUSES` | e.g. `401` (a 403 will not lock a user out) |
| On Redis outage | `RATELIMIT_DEGRADE` | `local`(default) · `closed` · `open` |

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
