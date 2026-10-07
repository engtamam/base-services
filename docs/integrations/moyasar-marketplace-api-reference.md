# Moyasar — Marketplace / Split API Reference

> Reference for the `moyasar` marketplace adapter in `payments-service` (Phase 2).
> Source: docs.moyasar.com, captured 2026-10-07. **Re-verify every shape at build** — items marked
> **CONFIRM** were not fully exposed in the captured excerpts.

Moyasar Financial is a **SAMA-licensed Payment Institution** (licence to 2030-01-13).

## Auth / base
- Base `https://api.moyasar.com/v1`. **HTTP Basic** auth with the platform secret key as the username
  (empty password) — our `MARKETPLACE_SECRET_KEY`. **CONFIRM** exact scheme.

## Amounts
- **Minor units** (halalas): 1.00 SAR = 100. So NO major-unit conversion (unlike Tap) — pass our
  `amount_minor` straight through.

## Split payment
Create Payment (`POST /payments`) — and/or a hosted Invoice (`POST /invoices`, **CONFIRM** which carries
splits for a hosted flow) — takes a `splits` array:
```json
"splits": [
  { "amount": 9000, "recipient_id": "<uuid>", "recipient_type": "Entity",
    "reference": "...", "fee_source": false, "refundable": true }
]
```
- `recipient_type` ∈ `Entity | Platform | Beneficiary`; `recipient_id` is the sub-merchant's id.
- `amount` is minor units; the platform's share is the remainder (same model as Tap).

## Statuses
Webhook events: `payment_paid`, `payment_failed`, `payment_refunded`, `payment_voided`,
`payment_authorized`, `payment_captured`, `payment_verified`. **`paid` is the success**.

## Payout
`POST /payouts` (payout account id as source; destination type/IBAN/name). `GET /payouts/{id}` for status.
**CONFIRM** payout status enum.

## Webhook verification
- The endpoint has a consumer-assigned **`secret_token`**; the webhook carries it so the receiver can
  authenticate the call (header `x-moyasar-signature` or a body field — **CONFIRM**). Verify by a
  timing-safe compare against our stored `MARKETPLACE_WEBHOOK_SECRET`, AND re-fetch the payment
  (verify-by-fetch) as the trust anchor (same discipline as Tap/TuwaiqPay). Idempotent apply.

## Adapter mapping (`moyasar` adapter)
- Declares `model=marketplace, capabilities={checkout, onboarding, split, payout}, onboarding={api}`
  (hosted/mode-B for Moyasar is **CONFIRM** — declare `{api}` until confirmed).
- `prepare_split_checkout` → create hosted invoice/payment with `splits` (minor amounts).
- `verify_webhook` → secret_token timing-safe compare + verify-by-fetch.
- `fetch_status` → GET payment/invoice; paid → succeeded.
- `onboard_merchant` (api) → create entity/beneficiary → recipient id. `fetch_payout_status` → GET payout.
- Credentials: the platform MARKETPLACE_* store (Basic-auth secret key).

## CONFIRM at build
1. Basic-auth exact form; hosted-checkout endpoint for splits (payments vs invoices) + the returned url field.
2. Webhook secret_token transport (header vs body) + exact field.
3. Payout status enum; onboarding/recipient-creation endpoints; whether hosted onboarding exists.
