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
| internal | `POST /internal/billing/webhook/{provider}` | gateway webhook (signature = auth) |
| internal | `POST /internal/billing/dunning` | run the dunning sweep |

Admin = `platform.billing` (cross-tenant) / `billing.manage` (own tenant); customer = `billing.self`.

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
