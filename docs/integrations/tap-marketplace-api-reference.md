# Tap Payments — Marketplace / Split API Reference

> Reference for building the `tap` marketplace adapter in `payments-service` (Phase 1).
> Source: Tap developer docs (developers.tap.company), captured 2026-10-07.
> **Re-verify every shape against the live first-party docs at build time** — several details below were
> not fully exposed in the captured excerpts and are marked **CONFIRM**. Do not build from guesses.

Tap Financial is a **SAMA-licensed Payment Institution** (licence to 2029-09-04; also KW/UAE). It holds
the funds and pays out to sub-merchants, which is what keeps the consuming platform out of the
fund-holding licence (the platform's split role is still a contract/adviser item).

## Auth
- `Authorization: Bearer <secret_key>` (the platform's marketplace secret key). **CONFIRM** exact header.

## Onboarding a sub-merchant (marketplace)
Two modes:
- **API (mode A):** create a Lead (Retailer) → Business (Files API for KYC docs) → Merchant → Destination.
  In KSA all data is validated via trusted third parties **even in test** (invalid CR/National ID → fail).
- **Hosted (mode B):** **JS Connect** — the merchant signs up and uploads KYC **directly to Tap** from
  within our UI (React NPM package or JS lib); we never handle the documents, only receive the id/status.

### Create a Destination (the payout target we store)
Request fields:
```json
{
  "display_name": "destplay",
  "business_id": "bus_...",
  "business_entity_id": "bsa_...",
  "brand_id": "brd_...",
  "branch_id": "brc_...",
  "bank_account": {"iban": "KW..."},
  "settlement_by": "Acquirer"
}
```
Response carries the **destination `id`** — this is the ONLY thing we persist (`tenant → destination id`),
never the IBAN/docs.

## Split payment (charge-level)
Add a `destinations` object to the Create Charge request:
```json
"destinations": {
  "destination": [
    { "id": "480593", "amount": 2, "currency": "SAR" }
  ]
}
```
- Each entry = destination `id` + `amount` (allocated to that sub-merchant) + `currency`.
- **The remaining balance stays with the master (platform) account** — i.e. the platform fee/commission is
  the IMPLICIT remainder, `total - sum(destination amounts)`, not an explicit field.
- Our server-authoritative invariant: `sum(destination amounts) <= total` and
  `platform_remainder = total - sum(destination amounts) >= 0` (fail-closed; refuse otherwise).

## Charge statuses
`INITIATED, ABANDONED, CANCELLED, FAILED, DECLINED, RESTRICTED, CAPTURED, VOID, TIMEDOUT, UNKNOWN`.
- `INITIATED` → redirect the customer to the payment URL.
- `CAPTURED` → paid (the only success).
- `FAILED/DECLINED/CANCELLED/VOID/...` → not fulfilled.

## Payout (settlement) statuses
`PENDING, INITIATED, FAILED, PAID_OUT`. Payouts API lists settlements Tap deposits to the business bank
account, with a download endpoint for reconciliation.

## Webhook verification
- Tap signs webhooks with a **hashstring (HMAC-SHA256)**. Captured excerpts did **not** fully specify the
  signed-field order / header name / amount formatting. Community/older docs describe an ordered concat of
  `x_id, x_amount, x_currency, x_gateway_reference, x_payment_reference, x_status, x_created` with the
  amount formatted to the currency's decimals. **CONFIRM the exact scheme + header name with Tap.**
- **Trust anchor = re-fetch the charge (Retrieve a Charge) before mutating** — metadata/customer are not
  signed and the posted body must not be trusted for attribution. This is the same verify-by-fetch
  discipline our TuwaiqPay adapter uses, so the adapter's `verify_webhook` does: compute+compare the
  hashstring when the scheme is confirmed AND/OR re-fetch the charge and treat the fetched status as truth.
  Webhooks can fire twice → application must be idempotent.

## Adapter mapping (payments-service `tap` adapter, Phase 1)
- Declares `model=marketplace, capabilities={checkout, onboarding, split, payout}, onboarding={api, hosted}`.
- `prepare_split_checkout(destinations=[{id, amount, currency}], ...)` → Create Charge with `destinations`.
- `onboard_merchant(...)` → Lead/Business/Merchant/Destination (mode A) OR return a JS Connect handle (mode B).
- `fetch_payout_status(...)` → Payouts API.
- `verify_webhook(...)` → hashstring compare (when confirmed) + verify-by-fetch (Retrieve a Charge).
- Credentials: the PLATFORM's marketplace secret key (platform-scoped store, NOT per-tenant MERCHANT_*).

## Gaps to CONFIRM at build
1. Exact webhook hashstring field order + header name + amount formatting.
2. Exact Create Charge full body (customer, source, redirect/post URLs) for KSA.
3. JS Connect embed contract + the onboarding-status callback shape (source of `merchant.onboarding_updated`).
4. Destination creation prerequisites (business/brand/branch ids) and whether a simpler "lead" path exists.

## Support / docs
developers.tap.company · https://developers.tap.company/reference/destinations ·
https://developers.tap.company/reference/charges · https://developers.tap.company/docs/marketplace-getting-started
