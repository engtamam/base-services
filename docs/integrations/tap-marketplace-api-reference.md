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

## Webhook verification (CONFIRMED 2026-10-07)
- Tap signs webhooks with a **hashstring = HMAC-SHA256 keyed with the SECRET KEY** (not a separate webhook
  secret), sent in the **`hashstring`** header, over the ordered `x_`-labelled concat:
  `x_id{id}x_amount{amount}x_currency{currency}x_gateway_reference{..}x_payment_reference{..}x_status{status}x_created{created}`,
  with the **amount formatted to the currency's decimals** (3 for KWD/BHD/OMR/JOD/IQD/TND, else 2).
- **Trust anchor = re-fetch the charge (Retrieve a Charge) before mutating** — metadata/customer are not
  signed. `verify_webhook` compares the `hashstring` (timing-safe, keyed by the secret key) when present
  AND re-fetches the charge, treating the fetched status as truth. Webhooks can fire twice → idempotent.
- Implemented in `adapters/payment/tap.py` (`_hashstring` keys with `self._secret`). Covered by tests.

## Adapter mapping (payments-service `tap` adapter, Phase 1)
- Declares `model=marketplace, capabilities={checkout, onboarding, split, payout}, onboarding={api, hosted}`.
- `prepare_split_checkout(destinations=[{id, amount, currency}], ...)` → Create Charge with `destinations`.
- `onboard_merchant(...)` → Lead/Business/Merchant/Destination (mode A) OR return a JS Connect handle (mode B).
- `fetch_payout_status(...)` → Payouts API.
- `verify_webhook(...)` → hashstring compare (when confirmed) + verify-by-fetch (Retrieve a Charge).
- Credentials: the PLATFORM's marketplace secret key (platform-scoped store, NOT per-tenant MERCHANT_*).

## Version (CONFIRMED 2026-10-07)
- API `https://api.tap.company/v2` is current (no v3); Bearer auth with the secret key. Our adapter uses this.

## Gaps to CONFIRM at build
1. ~~Webhook hashstring field order + header name + amount formatting~~ — CONFIRMED above.
2. Exact Create Charge full body (customer, source, redirect/post URLs) for KSA.
3. JS Connect embed contract + the onboarding-status callback shape (source of `merchant.onboarding_updated`).
4. Destination creation prerequisites (business/brand/branch ids) and whether a simpler "lead" path exists.

## Support / docs
developers.tap.company · https://developers.tap.company/reference/destinations ·
https://developers.tap.company/reference/charges · https://developers.tap.company/docs/marketplace-getting-started
