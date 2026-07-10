# Phase 14 — billing-service (detailed steps)

> **Goal:** a **general, plug-and-play, smart** billing & subscriptions service (SaaS
> checklist §8). Plans/prices, subscription lifecycle (trial→active→cancel + proration),
> usage metering, a pricing engine (flat/per-seat/usage/hybrid), an invoicing engine
> (sequential numbering + line items + **PDF EN/AR**), **payments across many KSA +
> international gateways** behind one `PaymentGateway` port, dunning, VAT tax, promo
> codes — and it **settles → calls zatca-service `/internal/zatca/finalize`** to
> e-invoice. Config-driven, multi-tenant.
>
> _Pairs with [[zatca-service]] (e-invoicing) + notification (receipts/dunning) + tenant. Memory: [[ksa-market]]._
> _Rules: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 14)

- [ ] `services/billing-service` generated from `_template` (self-contained).
- [ ] **`PaymentGateway` port** with **many** adapters, each **verified vs official docs**: KSA — **Moyasar, Tap, HyperPay, PayTabs** (Mada/Apple Pay/STC via them); international — **Stripe, PayPal, Checkout.com**; plus **`null`** (fail-closed). charge / refund / verify_webhook.
- [ ] **`TaxCalculator` port** (`vat` config-rate incl. KSA 15% + exemption/reverse-charge · `null`).
- [ ] **`EInvoicer` port** (`zatca-http` → posts to zatca-service `/internal/zatca/finalize` · `null`) — the billing↔zatca tie-in.
- [ ] **`PdfRenderer` port** (`null` · `reportlab` EN/AR bilingual, optional `[pdf]` extra).
- [ ] **Plans & prices** (flat / per-seat / usage / hybrid) + **pricing engine**.
- [ ] **Subscription lifecycle** — trial, activate, renew, pause, cancel, reactivate, change-plan with **proration**.
- [ ] **Usage metering** (record + aggregate per period, limits/alerts).
- [ ] **Invoicing** — auto-generate on cycle, **sequential per-tenant numbering**, line items, coupons, tax, credit notes; then **e-invoice via zatca**.
- [ ] **Dunning** — failed-payment retry schedule (backoff), notify, grace, suspend/reactivate.
- [ ] **Promo codes** — %/fixed/trial-extension, expiry + max redemptions.
- [ ] **Webhooks** — verify + reconcile gateway payment events (idempotent).
- [ ] **Security** — no raw card storage (tokenized), gateway secrets via config/vault, webhook signature verified, secret-free logs, CWE-guard egress, deny-by-default caps, tenant-scoped.
- [ ] **Zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot (subscribe→invoice→charge(mock)→e-invoice(mock zatca)→dunning→webhook reconcile).

---

## 1. Prerequisites & decisions

1. **One-time provider verification** (the notification pattern): confirm each gateway's charge/refund/webhook endpoint, auth, and signature scheme against official docs before coding its adapter. Record confirmed facts inline.
2. **Never store raw cards** — adapters take a provider **token/source** (PCI SAQ-A). Only tokens + last4/brand persist.
3. **Money is integer minor units** end-to-end (halalas/cents); Decimal for tax/proration; no float drift.
4. **Zatca tie-in is a port** — `EInvoicer` (`zatca-http`) so billing stays decoupled; `null` in dev.
5. **PDF is here** (per the earlier decision) — `PdfRenderer` EN/AR; optional `[pdf]` extra (reportlab BSD); `null` default → reuse template venv.

---

## 2. Steps

### Step 14.1 — Skeleton
- `python scripts/create_service.py billing-service --desc "Billing, subscriptions & payments (KSA + international)"`.
- Add optional extras: `[pdf]` (reportlab + arabic-reshaper + python-bidi). Default drivers keep zero-new-deps → reuse template venv. Confirm inherited tests + gate green.

### Step 14.2 — Config (zero-hardcoded)
- `PAYMENT_GATEWAY_DRIVER` (null) + per-provider keys/secrets; `TAX_CALCULATOR_DRIVER` (vat) + `VAT_RATE` (0.15); `EINVOICER_DRIVER` (null) + `ZATCA_SERVICE_URL`/token; `PDF_RENDERER_DRIVER` (null); `CURRENCY` (SAR); dunning `DUNNING_RETRY_*` + `GRACE_DAYS`; `TRIAL_DAYS`. `.env.example`.

### Step 14.3 — Ports
- `PaymentGateway` (charge/refund/verify_webhook), `TaxCalculator` (compute), `EInvoicer` (finalize), `PdfRenderer` (render invoice → bytes).

### Step 14.4 — Payment adapters (verified) + registry
- `null` (fail-closed) + **Stripe** (PaymentIntents; Bearer secret key; webhook `Stripe-Signature` HMAC) + **Moyasar** (KSA; Basic secret; `/v1/payments`) + **Tap** (KSA; Bearer; `/v2/charges`) + **HyperPay** (KSA; Bearer entityId; `/v1/payments`) + **PayTabs** (KSA; `Authorization` server key; `/payment/request`) + **PayPal** (OAuth2; Orders v2) + **Checkout.com** (Bearer sk; `/payments`). Extend container (allowlist, fail-fast).
- **Verify each endpoint/auth/webhook vs official docs first.**

### Step 14.5 — Tax + EInvoicer + PDF adapters
- Tax: `vat` (config rate, exemption + B2B reverse-charge = 0) · `null`. EInvoicer: `zatca-http` (stdlib POST to `/internal/zatca/finalize`, X-Internal-Token) · `null`. PDF: `reportlab` (EN/AR, arabic-reshaper+bidi, lazy `[pdf]`) · `null`.

### Step 14.6 — Domain models + stores
- Models: `Plan`, `Price`, `Coupon`, `Subscription`, `Invoice`, `LineItem`, `UsageRecord`, `Payment`. Stores over `DataStore`; **sequential invoice number** per tenant (indexed counter).

### Step 14.7 — Core BillingService (catalog + subscriptions)
- Plan/coupon CRUD; `subscribe` (trial/active), `change_plan` (+proration), `pause`/`cancel`/`reactivate`; `record_usage` + aggregate; pricing engine (flat/per-seat/usage/hybrid); coupon apply.

### Step 14.8 — Core: invoicing + payments + dunning + zatca
- `run_cycle` → build Invoice (base+usage+proration−discount+tax, sequential number) → **charge** via gateway → paid → **e-invoice** via `EInvoicer` (zatca) → PDF; on failure → **dunning** (retry backoff, grace, suspend). `handle_webhook` reconciles paid/failed/refunded idempotently. Credit notes + refunds.

### Step 14.9 — Routes + manifest
- Admin (`platform.billing`/`billing.manage`): plans/coupons CRUD, list subs/invoices, run cycle, dunning run. Customer (`billing.self`): subscribe/change/cancel, invoices, usage, invoice PDF. Internal: `POST /internal/billing/webhook/{provider}`, `/run`, `/usage`. Wire rate_limit, deny-by-default, guard respond+egress. Manifest.

### Step 14.10 — Tests
- Unit: pricing (all models), proration, coupon, tax (VAT/exempt/reverse), invoice numbering, dunning backoff, webhook reconcile idempotent, each gateway request shape (mock transport), e-invoice call. Integration: subscribe→run→charge(mock)→paid→e-invoice(mock)→invoice/PDF; failed→dunning; webhook; cap-gating; tenant scope. Security: no raw card, secret-free, webhook signature required, deny-by-default.

### Step 14.11 — Verify + live boot
- `pytest` + gate green. Live: create plan → subscribe → run cycle → mock charge → paid → mock zatca finalize called → dunning on failure → webhook reconcile.

### Step 14.12 — Docs
- README (ports, gateways matrix, lifecycle, zatca tie-in, PDF EN/AR, security). Update architecture-plan + build-progress memory.

---

## 3. Out of scope (deferred)

- Full **subscription scheduler/cron** (a `/internal/billing/run` endpoint; scheduler at deploy).
- **3-D Secure / hosted checkout redirect flows** (adapters expose the intent; UI drives redirect).
- **Live gateway onboarding** (real keys) — exercised against mock transports.
- **Multi-currency FX** (per-currency prices supported; no conversion).

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #145 | 14.1 Skeleton | ✅ completed |
| #146 | 14.2 Config | ✅ completed |
| #147 | 14.3 Ports | ✅ completed |
| #148 | 14.4 Payment adapters (verified) | ✅ completed |
| #149 | 14.5 Tax + EInvoicer + PDF adapters | ✅ completed |
| #150 | 14.6 Domain models + stores | ✅ completed |
| #151 | 14.7 Core catalog + subscriptions | ✅ completed |
| #152 | 14.8 Core invoicing + payments + dunning + zatca | ✅ completed |
| #153 | 14.9 Routes + manifest | ✅ completed |
| #154 | 14.10 Tests | ✅ completed |
| #155 | 14.11 Verify + live boot | ✅ completed |
| #156 | 14.12 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (141 tests; gate PASS; full lifecycle live) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is general + smart

| Choice | Knob | Options |
|---|---|---|
| Gateway | `PAYMENT_GATEWAY_DRIVER` | null · stripe · moyasar · tap · hyperpay · paytabs · paypal · checkout |
| Tax | `TAX_CALCULATOR_DRIVER` | vat · null |
| E-invoice | `EINVOICER_DRIVER` | null · zatca-http |
| PDF | `PDF_RENDERER_DRIVER` | null · reportlab (EN/AR) |
| Storage | `DATASTORE_DRIVER` | memory · postgres |

One billing core; swap the gateway/tax/e-invoice/PDF by config. **Any** SaaS — Saudi or
global — drops it in, points `EINVOICER` at zatca-service, and is ZATCA-compliant.

_When every box is ✅, billing-service is live — Tier-2, KSA + international._
