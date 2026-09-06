# TuwaiqPay — Payment Gateway API Reference

> Saved reference for building a future `tuwaiqpay` billing-service payment adapter.
> Source: TuwaiqPay Developer Documentation v2.0 (provided by the platform owner, captured 2026-09-06).
> This is the FIRST-PARTY reference to build against — do not build the adapter from guesses.

Saudi payment gateway. Supports MADA, VISA/Mastercard, Apple Pay, STC Pay, Amex. PCI DSS.
Currency: SAR (also USD, EUR). Bill/link-based flow (hosted payment page), not a direct card charge.

## Onboarding (out of band)
Register at tuwaiqpay.com.sa → verify (email + mobile) → activation (1–3 days) → receive API
credentials (username = `+966XXXXXXXXX`, password, environment). **No merchant dashboard exists** —
the account is managed via their app only; a Webhook URL is registered by emailing support.
Requirements: activated account, HTTPS/SSL on the merchant site, PHP 7.4+/Python 3.7+/Node 14+.

## Environments / Base URLs
| | UAT (testing) | Production |
|---|---|---|
| Base URL | `https://onboarding-uat.tuwaiqpay.com.sa/api/v1` | `https://onboarding-prod.tuwaiqpay.com.sa/api/v1` |
| Cards | test | real |
| Funds | not charged | charged |

⚠️ **UAT timezone bug:** UAT returns `expireDate` in Saudi time (UTC+3); add +3h when persisting
in UAT. (Note for the adapter: treat `expireDate`/`paidAt` tz carefully; prefer the webhook/status
as the source of truth over the local expiry clock.)

## Auth — `POST /auth/authenticate`
Headers: `Content-Type: application/json`, `Accept: application/json`, `X-Language: ar`
Body:
- `username` (string, required) — mobile `+966XXXXXXXXX` OR email. Local `05...` is NOT accepted.
- `userNameType` (enum, required) — `MOBILE` | `EMAIL`
- `password` (string, required)

Success 200:
```json
{ "data": { "access_token": "eyJ...", "refresh_token": "ea33...", "expires_in": 3600,
            "token_type": "Bearer" }, "message": "SUCCESS" }
```
Errors: 401 invalid credentials · 404 user not registered · 423 account locked.
Token TTL 3600s — cache it (auth is a separate call, not per-charge). Refresh token provided.

## Create bill (the "charge") — `POST /integration/bills`
Headers: `Authorization: Bearer {access_token}` + the JSON/Accept/X-Language headers.
Body:
- `amount` (float, required) — SAR, e.g. `150.00`
- `actionDateInDays` (int, required) — bill validity 1–365
- `currencyId` (int, required) — `1`=SAR, `2`=USD, `3`=EUR
- `supportedPaymentMethods` (string[], required) — any of `VISA MASTER MADA AMEX STC_PAY APPLE_PAY`
- `description` (string, required)
- `customerName` (string, required)
- `customerMobilePhone` (string, required) — `+966XXXXXXXXX`
- `customerEmail` (string, optional)
- `includeVat` (bool, optional, default false)

Success 200:
```json
{ "data": {
    "transactionId": "70f056c37a8daee0c4af36adabf97614",
    "merchantTransactionId": "HpMDa1CNKvn5_1775410290289_101550",
    "billId": "101550",
    "link": "https://uat-payment.tuwaiqpay.com.sa/pay/WN6q5ErU",
    "qrCode": "https://...",
    "amount": 150.00, "currency": "SAR",
    "expireDate": "2026-04-15T20:31:30", "status": "PENDING"
  }, "message": "SUCCESS" }
```
- `link` — hosted payment page to send the customer to.
- ⭐ `merchantTransactionId` — the key to persist and correlate the webhook to the bill.
- `transactionId` — used to query status.

## Check payment — `GET /integration/bills/transaction/{transactionId}`
Success 200: `data.transactionStatus` + `data.bill{amount,currency}` + `data.paymentMethod` + `data.paidAt`.

### Transaction statuses
| status | meaning |
|---|---|
| `PENDING` | not paid yet |
| `SETTLED` / `APPROVED` / `PENDING_SETTLEMENT` | PAID ✅ |
| `FAILED` / `DECLINED` | failed ❌ |
| `CANCELLED` | cancelled |

## Webhooks (payment status change)
TuwaiqPay POSTs to a merchant URL on status change. **No dashboard** — register the Webhook URL by
emailing support (support@tuwaiqpay.com.sa / wa.me/966509684000) with the URL, the events wanted
(Completed/Failed/Cancelled), and the Merchant ID; they enable it.

Payload:
```json
{ "transactionDetails": {
    "id": 1412,
    "transactionId": "1549e858574155fca7f5abd3e164c275",
    "merchantTransactionId": "HpMDa1CNKvn5_1775583254771_101562",
    "transactionStatus": "PENDING_SETTLEMENT",
    "bill": { "id": 101562, "amount": 150.00, "currency": { "nameEn": "SAR" } },
    "paymentMethod": { "code": "VISA" },
    "paidAt": "2026-04-12T18:30:00"
  } }
```
**Webhook verification:** the docs describe NO signature/HMAC scheme — only advice to check the source
IP and to verify the amount against the stored bill (`amount mismatch`). This is a real gap for our
adapter's `verify_webhook`: without a signed secret we cannot cryptographically verify. Options when
building: (a) confirm each webhook by calling Check-payment (`transactionId`) before trusting it —
the safe path — and/or (b) source-IP allowlist. Confirm with TuwaiqPay whether a signing secret is
available before shipping; do not assume one.

## Error codes
200 ok · 400 bad request (missing/invalid) · 401 unauthorized (bad password) · 403 forbidden (**API
not enabled for the account — most common; email support to enable API Integration**) · 404 user not
registered · 423 account locked · 500 server error (retry).

## Notes for the adapter build (base-services `billing-service`)
- Shape is bill/link + poll/webhook, NOT a synchronous token charge like Stripe/Moyasar. Our
  `PaymentGateway` port centres on `charge(token, ...)` returning a provider id. TuwaiqPay has no
  saved-token reusable charge in these docs — it mints a hosted-payment link per bill. This affects
  subscription RENEWALS: a card-on-file recurring charge may not be supported by this API as written.
  → Clarify recurring/token support with TuwaiqPay before wiring it to subscriptions; it may suit
  one-off/settlement flows first. It slots into the existing driver registry + activation/pinning
  machinery either way.
- Auth is username/password → access_token (Bearer), token cached ~3600s. Our adapter would hold the
  credentials as its secret set (PAYMENT_API_KEY etc. mapping TBD — likely username+password, so it
  may need two credential fields).
- `verify_webhook`: no signature scheme documented → verify by re-querying transaction status; treat
  the webhook as a hint, the GET as the truth.
- Map statuses: SETTLED/APPROVED/PENDING_SETTLEMENT → paid; FAILED/DECLINED → failed; CANCELLED →
  cancelled; PENDING → pending.

## Support
support@tuwaiqpay.com.sa (24/7) · +966 50 968 4000 · wa.me/966509684000 · tuwaiqpay.com.sa
