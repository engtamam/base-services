# Moyasar — Marketplace / Split API Reference

> Adapter: `services/payments-service/app/adapters/payment/moyasar.py`. **Every row cites an explicit
> first-party source + capture date. No detail is assumed** — anything not confirmed is labelled
> **UNVERIFIED** and must be confirmed against the live docs / a sandbox account before the marketplace
> model is used in production. Latest API as of capture: **v1**. Captured **2026-10-07**.

Moyasar Financial is a SAMA-licensed Payment Institution.

| Item | Value | Status | Source (captured 2026-10-07) |
|---|---|---|---|
| Base/version | `https://api.moyasar.com/v1` | ✅ Verified | docs.moyasar.com/api/api-introduction |
| Auth | HTTP Basic, API key = username, empty password | ✅ Verified | docs.moyasar.com/api/authentication |
| Amount unit | minor (halalas): 1.00 SAR = 100 | ✅ Verified | docs.moyasar.com/api/payments/01-create-payment ; /api/invoices/01-create-invoice |
| Create invoice (hosted) | `POST /v1/invoices` → response `url` is the hosted checkout page | ✅ Verified | docs.moyasar.com/api/invoices/01-create-invoice |
| Invoice redirect fields | `success_url` (redirect when paid), `back_url` (back button), `callback_url` (server notification, not a redirect) | ✅ Verified | docs.moyasar.com/api/invoices/01-create-invoice |
| Split | `splits[] = {amount(minor), recipient_id(uuid), recipient_type: Entity\|Platform\|Beneficiary, reference, description, fee_source, refundable}` | ✅ Verified (on `/payments`) | docs.moyasar.com/api/payments/01-create-payment |
| Split on **invoices** (hosted) | `splits` returned only for entities created after 2025-10; **request-side splits need account enablement (contact Moyasar support)** | ⚠️ **Gated / confirm per account** | docs.moyasar.com/api/invoices/01-create-invoice ("contact support team") |
| Fetch invoice | `GET /v1/invoices/{id}` → `status` | ✅ Verified | docs.moyasar.com/api/invoices/04-show-invoice |
| Payment statuses | initiated, paid, authorized, captured, verified, failed, refunded, voided, expired (paid = success) | ✅ Verified | docs.moyasar.com/api/payments/01-create-payment ; /api/other/webhooks/available-webhooks |
| Webhook verification | `X-Moyasar-Signature` = HMAC-SHA256 of the RAW body (hex) keyed with our secret, OR body `secret_token` = our secret | ✅ Verified | docs.moyasar.com/api/other/webhooks/webhook-reference ; help.moyasar.com/.../moyasar-dashboard-webhooks |
| Payout object/status | `GET /v1/payouts/{id}`; statuses: queued, initiated, paid, failed, canceled, returned | ✅ Verified | docs.moyasar.com/guides/payouts/payout-details ; /api/payouts/04-create-payout |
| Onboarding — recipient (Entity) `POST /v1/entities` | `recipient_id` must be a valid Entity/Platform/Beneficiary UUID | ⚠️ **UNVERIFIED** (that `recipient_id` is such a UUID is confirmed; the `POST /v1/entities` creation endpoint is NOT confirmed — may be dashboard/another API) | docs.moyasar.com/api/payments/01-create-payment (recipient_id) ; entity-creation endpoint TBC |
| Hosted onboarding (mode B) | — | ⚠️ **UNVERIFIED** (declare `onboarding={api}` only until confirmed) | — |

**Trust anchor:** `verify_webhook` compares whichever credential is present (HMAC header or body
secret_token), timing-safe, then re-fetches the payment/invoice and trusts the fetched status — so a
forged/replayed body settles nothing. Idempotent apply.

## Still to confirm before GA (no assumptions shipped as fact)
1. Request-side `splits` on `/invoices` (hosted) vs `/payments` (tokenized source) — enablement with Moyasar.
2. Entity/recipient CREATION endpoint (adapter currently posts `/v1/entities` — UNVERIFIED).
3. Whether a hosted (mode B) onboarding exists.
