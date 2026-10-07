# Tap Payments — Marketplace / Split API Reference

> Adapter: `services/payments-service/app/adapters/payment/tap.py`. **Every row below cites an explicit
> first-party source and the date it was captured. No detail is assumed** — anything not confirmed from
> Tap's own docs is labelled **UNVERIFIED** and must be confirmed against the live docs / a sandbox
> account before the marketplace model is used in production. Latest API as of capture: **v2** (charges)
> and **v3** (lead). Captured **2026-10-07**.

Tap Financial is a SAMA-licensed Payment Institution (also KW/UAE).

| Item | Value | Status | Source (captured 2026-10-07) |
|---|---|---|---|
| Charges base/version | `https://api.tap.company/v2` | ✅ Verified | developers.tap.company/reference/charges ; docs/get-started |
| Auth | `Authorization: Bearer <secret_key>` | ✅ Verified | developers.tap.company/reference/api-endpoint (API Authentication) |
| Create charge | `POST /v2/charges` | ✅ Verified | developers.tap.company/reference/create-a-charge |
| Charge required fields | `amount`, `currency`, `customer{first_name,email}`, `source{id}`, `redirect{url}` | ✅ Verified | developers.tap.company/reference/create-a-charge |
| Hosted all-methods source | `source.id = "src_all"` (all enabled methods on the hosted page; `src_card` = cards only) | ✅ Verified | support.tap.company/.../153000140215 and .../153000140236 |
| Split | `destinations.destination[] = {id, amount, currency}`; platform keeps the remainder | ✅ Verified | developers.tap.company/reference/charges ; /reference/destinations ; /docs/marketplace-overview |
| Charge statuses | INITIATED, ABANDONED, CANCELLED, FAILED, DECLINED, RESTRICTED, CAPTURED, VOID, TIMEDOUT, UNKNOWN (CAPTURED = success) | ✅ Verified | developers.tap.company/reference/charges |
| Retrieve charge | `GET /v2/charges/{id}` (verify-by-fetch anchor) | ✅ Verified | developers.tap.company/docs/recommendations-best-practices (Retrieve a Charge) |
| Webhook signature | `hashstring` header = HMAC-SHA256 keyed with the **secret key** over ordered `x_id,x_amount,x_currency,x_gateway_reference,x_payment_reference,x_status,x_created`; amount to currency decimals (3 for KWD/BHD/OMR/JOD/IQD/TND, else 2) | ✅ Verified | developers.tap.company/docs/webhook |
| Onboarding — Create Lead | `POST /v3/lead` (brand name en, national id, commercial_registration, iban, marketplace MID) → `{id: led_...}` | ✅ Verified | developers.tap.company/reference/create-a-lead-for-retailer |
| Onboarding — destination | KYC upload → **Create Destination (Business API)** yields the `destination_id`; payouts unavailable while the business is pending approval | ✅ Verified (chain exists) / ⚠️ exact Create-Destination request **UNVERIFIED** | developers.tap.company/docs/marketplace-getting-started ; /reference/destinations |
| Payouts — list / download | `GET /v2/payouts/list`, `GET /v2/payouts/download` | ✅ Verified | developers.tap.company/reference/list-payouts ; /reference/download-payouts |
| Payouts — single status `GET /v2/payouts/{id}` | used by `fetch_payout_status` | ⚠️ **UNVERIFIED** (list/download confirmed; single-GET path not found in docs) | developers.tap.company/reference/payout (to confirm) |
| JS Connect (hosted onboarding, mode B) | merchant signs up + uploads KYC directly to Tap, in-page | ✅ Verified (exists) / ⚠️ embed contract + onboarding-status callback **UNVERIFIED** | support.tap.company/.../153000140181 ; developers.tap.company/docs/creating-a-lead |

**Trust anchor:** `verify_webhook` compares the `hashstring` (timing-safe, keyed by the secret key) when
present AND re-fetches the charge (`GET /v2/charges/{id}`), treating the fetched status as truth — so a
forged/replayed body settles nothing even if a signature detail drifts. Webhooks can fire twice →
application is idempotent.

## Still to confirm before GA (no assumptions shipped as fact)
1. Create-Destination (Business API) exact request/response for the api-onboarding chain.
2. Single-payout status endpoint path (`fetch_payout_status`) — confirm `GET /v2/payouts/{id}` vs list-only.
3. JS Connect embed contract + the onboarding-status callback shape (source of `merchant.onboarding_updated`).
