# billing-service

> Self-contained, **general & plug-and-play** billing & subscriptions service (SaaS
> checklist §8). Plans/prices, subscription lifecycle (trial→active→cancel + proration),
> usage metering, a pricing engine (flat/per-seat/usage/hybrid), invoicing (sequential
> numbering + **PDF EN/AR**), **payments across many KSA + international gateways**,
> dunning, VAT tax, promo codes — and it **settles → calls zatca-service to e-invoice**.
> Multi-tenant, config-driven, zero new runtime deps (extras optional). Backend only.

---

## Payment gateways (one port, verified vs official docs)

| Region | Gateways |
|--------|----------|
| **KSA / GCC** | **Moyasar**, **Tap**, **HyperPay**, **PayTabs**, **Geidea**, **APS/PayFort** (Mada · Apple Pay · STC Pay via them) |
| **International** | **Stripe**, **PayPal**, **Checkout.com** |
| Dev | `null` (fail-closed) · `recording` (pilot-safe, no settlement) |

Swap with `PAYMENT_GATEWAY_DRIVER`. Adapters take a **provider token** — never a raw
card (PCI SAQ-A). Amount-unit differences are handled per adapter (Stripe/Moyasar/
Checkout/**APS** = minor units; Tap/HyperPay/PayTabs/**Geidea**/PayPal = major decimal),
with correct precision per currency (KWD/BHD/OMR = 3-dp).

Auth per provider: Bearer (Stripe/Tap/HyperPay/Checkout) · Basic (Moyasar/**Geidea**) ·
server-key header (PayTabs) · OAuth2 (PayPal) · **signed body** (APS: SHA-256 of
`phrase + sorted(name=value) + phrase`). Webhook signatures verified for each.

## Ports (config-selected)

| Port | Drivers | Role |
|------|---------|------|
| `PaymentGateway` | null · stripe · moyasar · tap · hyperpay · paytabs · paypal · checkout | charge / refund / verify_webhook |
| `TaxCalculator` | vat · null | VAT (KSA 15%), exemption, B2B reverse-charge |
| `EInvoicer` | null · zatca-http | **→ zatca-service `/internal/zatca/finalize`** |
| `PdfRenderer` | null · reportlab (EN/AR) | invoice PDF (optional `[pdf]` extra) |

## Lifecycle

```
subscribe (trial/active) → run cycle → build invoice (base+usage+proration−coupon+tax,
  sequential number) → CHARGE via gateway
     paid → e-invoice via zatca → (PDF on demand)
     failed → DUNNING (retry base·2ⁿ h, cap → suspend past_due, grace)
webhook (signature-verified) → reconcile paid/failed/refunded (idempotent)
```

## Pricing engine

- **flat** — fixed per period · **per-seat** — × quantity · **usage** — metered × overage
- **hybrid** — base + overage over included units · **coupons** — %/fixed/trial-extension
- **VAT-inclusive** prices (`includes_tax` — KSA display prices) split VAT out automatically

## Enforcement & money-safety (v2)

- **Entitlements** — general limit registry per plan (`-1/None`=unlimited, `0`=deny, `N`=limit).
  Any service asks `GET /internal/billing/entitlements/check?tenant=&key=&current=` before
  allowing an action — fail-closed. (`/api/v1/billing/entitlements/{key}` for the customer.)
- **Metered overage** — `POST /subscriptions/{id}/overage` bills usage over included units,
  idempotent per period; zero usage issues nothing.
- **Money ledger** — every charge/refund/credit is one immutable row with a running balance
  (`GET /ledger`) — the reconciliation source of truth.
- **Idempotency** — deterministic keys on the billing cycle/overage so a double-click or
  retry can't double-charge.
- **Multi-currency** — correct minor-unit precision (SAR/USD 2-dp, KWD/BHD/OMR 3-dp, JPY 0-dp).
- **Buyer tax identity** — `buyer_vat`/`buyer_cr` on the subscription flow into the e-invoice;
  optional B2B **reverse-charge** zeroes VAT.
- **Tax profile is one unit, and complete or nothing** — `BILLING_DOCUMENT_MODE` and the profile
  behind it (`TAX_CALCULATOR_DRIVER`, `VAT_RATE`, `INVOICE_SELLER_VAT`) are edited together from the
  operator panel. A mode that issues a **tax invoice** is refused — at the write **and** at render —
  unless a real calculator, a rate above zero, and a seller registration number are all present;
  the refusal **names the missing key** (`TAX_PROFILE_INCOMPLETE: INVOICE_SELLER_VAT`). What a paid
  invoice **is** (`receipt` | `tax_invoice`) is **frozen on the invoice** and the seller identity
  captured with it, so switching the mode — or switching back — never rewrites a document already
  issued, and a seller number emptied later makes a document **fall back to a receipt**, never a tax
  invoice missing its registration.
- **Recording mode + verify** — `PAYMENT_GATEWAY_DRIVER=recording` runs the flow without
  charging (pilot-safe); `GET /gateway/verify` confirms which gateway is wired without moving money.
- **Grandfathering** (`GRANDFATHER=true`) — a subscription captures its **price + limits at
  signup**. Raising prices or cutting limits in the catalogue **never silently re-prices or
  locks out an existing customer**; new signups get the new terms. Moving an existing
  customer is an explicit act: `POST /subscriptions/{id}/migrate` (a customer-chosen
  `change` also re-captures). Set `GRANDFATHER=false` to always bill the live catalogue.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| user | `POST/GET /api/v1/billing/plans` · `POST /coupons` | catalog (admin) |
| user | `POST /api/v1/billing/subscriptions` · `/{id}/change` · `/{id}/cancel` | subscribe / change / cancel |
| user | `POST /api/v1/billing/subscriptions/{id}/run` | run a billing cycle |
| user | `POST /api/v1/billing/usage` | record metered usage |
| user | `GET /api/v1/billing/invoices` · `/{id}/pdf` | invoices + PDF |
| user | `POST /api/v1/billing/payments/{id}/refund` · `/dunning/run` | refund / dunning |
| user | `POST /api/v1/billing/invoices/{id}/settle` | **off-gateway settlement** (bank transfer / cheque) |
| internal | `POST /internal/billing/webhook/{provider}` | gateway webhook (signature = auth) |
| internal | `POST /internal/billing/dunning` | run the dunning sweep |

Admin = `platform.billing` (cross-tenant) / `billing.manage` (own tenant); customer = `billing.self`.

### Off-gateway settlement (bank transfer / cheque — the KSA default)

Aramco vendors buy on a PO and pay by **bank transfer** against a ZATCA tax invoice, not a card.
`POST /invoices/{id}/settle` closes an invoice from a payment that never touched a gateway:

- Identity is `(method, issuer, reference)` — the same wire (e.g. `bank_transfer` / `alrajhi` /
  `TRX-9001`) can never settle twice, even across two admins or two tranches landing together.
- Uses the bank's **value date** as `paid_at` (not `now()`), so the ZATCA invoice files in the
  right tax period and revenue is recognised in the right month.
- Supports **partial** settlement with a tolerance; the last tranche within tolerance closes it.
- On close it marks the invoice paid, finalizes the e-invoice **and reactivates a `past_due`
  subscription** — exactly as a gateway webhook does, so the invoice and the entitlement never
  disagree.

Config: `SETTLEMENT_METHODS`, `SETTLEMENT_ALLOW_PARTIAL`, `SETTLEMENT_TOLERANCE_MINOR`,
`SETTLEMENT_MAX_BACKDATE_DAYS`. The entitlement in billing stays the one source of truth.

## ZATCA tie-in

On payment, billing hands the invoice to the `EInvoicer` port. With `EINVOICER_DRIVER=
zatca-http` it POSTs `{tenant, invoice}` to zatca-service `/internal/zatca/finalize` —
which signs (official SDK), clears/reports to Fatoora, and chains it. Non-fatal: an
unreachable zatca-service returns `deferred` (billing already collected the money).

## Security

Tokenized payments (**no raw card storage**); gateway secrets via config/vault; **webhook
signatures verified** (HMAC/field per provider); secret-free logs; CWE-guarded egress;
deny-by-default + capability RBAC; tenant-scoped.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci,pdf]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope

Subscription scheduler/cron (a `/run` endpoint; scheduler at deploy) · 3-DS / hosted
redirect flows (adapters expose the intent; UI drives) · live gateway onboarding
(exercised against mock transports) · multi-currency FX conversion.

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

> The subject is resolved **inside** the limiter (its own optional dependency), not read from
> pre-set request state — so an authenticated request keys per-subject regardless of the route's
> dependency order. No "one office NAT shares a bucket" surprise. Enforced by `safety:edge`.

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
