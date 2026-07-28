# Phase 49 — billing payments: runtime gateway, checkout, auto-reactivate, and a webhook that verifies

Four requests from the upstream (payment-integrating) team, plus two real bugs the review surfaced.
The theme is the same one this platform keeps returning to: make the payment layer **general and
plug-and-play** — a provider is chosen and swapped by config, a checkout is one port method every
adapter implements, and the money path has ONE truth every provider funnels into — while fixing the
places where a specific provider (HyperPay) quietly does the wrong thing.

## What already exists (so we build on it, not beside it)

- **Per-tenant credential resolution.** `rebuild_gateway` already lays operator-set credentials over
  the environment per tenant, and `resolver.gateway(tenant, …)` caches the result. A tenant already
  gets ITS OWN keys. What it does not yet get is its own *driver*.
- **One money truth.** `_mark_paid` is "the ONE place an invoice becomes paid" — a card charge, a
  webhook reconcile and an off-gateway settlement all land there, so entitlements, ZATCA e-invoicing
  and dunning behave identically for all three. Anything that should happen "when paid" belongs here,
  once, not in three callers.
- **A normalized gateway port.** `charge` / `refund` / `verify_webhook` return provider-neutral
  `Charge` / `WebhookEvent`. A new capability is a new port method every adapter maps to its provider,
  not a special case in the route.

## The belief this phase is built to destroy

> "The gateway is a deploy-time choice, a webhook is trustworthy because it arrived, and a paused
> customer who pays is reactivated by someone noticing."

Each clause is false in a way that costs money. The gateway is a per-tenant, change-after-launch
choice. A webhook is trustworthy only if it VERIFIES — and HyperPay's never did. And a customer who
pays to come back must come back automatically, the instant the money lands, not when an operator
happens to look.

## The work

### 49.1 — runtime, per-tenant gateway selection (plug-and-play switch)

Today `PAYMENT_GATEWAY_DRIVER` is read once at boot. Make it a per-tenant, runtime choice:

- Add `PAYMENT_GATEWAY_DRIVER` to `RUNTIME_POLICY`, so `/api/v1/operator/config` accepts it — and,
  because RUNTIME_POLICY is a PROMISE that something reads the key, wire the read in the SAME change.
- `rebuild_gateway` already receives the resolved per-tenant credential overlay; extend it to also
  read a per-tenant `PAYMENT_GATEWAY_DRIVER` override and pick the adapter accordingly. The resolver
  cache key becomes `(tenant, driver, credentials-generation)` so switching a tenant's provider takes
  effect on the next charge without a redeploy and without leaking the old gateway from cache.
- Fail-closed: an override naming a driver that is not configured (no keys) does not silently fall
  back to another provider's — it surfaces `GatewayUnavailable`, which dunning already treats as
  retryable, rather than charging through the wrong gateway.

Result: an operator moves a tenant from HyperPay to Geidea from the panel, audited, no deploy.

### 49.2 — customer payment-initiation (create checkout session)

We must never proxy provider keys through our own service. So add the initiation step every modern
gateway has — a server-created checkout/session the client widget or redirect then completes:

- New port method `prepare_checkout(*, amount_minor, currency, reference, return_url="", metadata=None)`
  returning a `CheckoutSession` (`id`, `provider`, `redirect_url`, `widget` config, `expires_at`).
- Adapters map it to their provider: **HyperPay** `POST /v1/checkouts` (returns the checkout id the
  COPYandPAY widget needs); **Geidea** create-session. An adapter with no server-side session (or the
  `null` driver) returns a clear `unsupported` rather than a broken half-answer — plug-and-play means
  a provider that cannot do this SAYS so, it does not fake it.
- A customer route `POST /api/v1/billing/checkouts` (tenant-scoped, rate-limited) resolves the
  tenant's gateway and returns `{id, provider, redirect_url, widget, expires_at}` — never a key.
- The completion still reconciles through the existing webhook / GET-status path into `_mark_paid`, so
  a prepared checkout that succeeds is the SAME paid invoice as a direct charge. No second money path.

### 49.3 — auto-reactivate a paused subscription on payment

A customer whose subscription paused for non-payment, then pays, must come back the moment the money
lands — from ANY payment path, because `_mark_paid` is the one place all three converge:

- In `_mark_paid`, after the payment is recorded (ledger + grant + event, in that order), if the
  invoice's subscription is `paused`, reactivate it. `reactivate` already refuses anything but
  `paused → active` and clears `cancel_at_period_end`, so a canceled or already-active subscription is
  untouched — the state machine, not this hook, guards correctness.
- Guarded by a runtime policy knob `BILLING_AUTO_REACTIVATE` (default on, in `RUNTIME_POLICY`) so an
  operator can turn it off per tenant; read at the point of use, never inert.
- Idempotent: a webhook re-delivery lands in `_mark_paid`'s existing idempotency, so reactivation
  fires once. The `subscription.reactivated` event is emitted by `reactivate`, so a consumer sees it.

### 49.4 — fix the HyperPay webhook (it never verified) — BUG

`HyperPay.verify_webhook` is a hard stub that returns `verified=False, type="unknown"` for every
webhook — so every HyperPay notification is unreconciled, and the platform relies entirely on
GET-status polling that may never run. Geidea's adapter verifies; HyperPay's must too.

- HyperPay webhooks are **AES-256-GCM encrypted**: the body is hex ciphertext, with the IV in
  `X-Initialization-Vector` and the GCM auth tag in `X-Authentication-Tag` (both hex), decrypted with
  the configured webhook key (hex). **The GCM auth tag IS the verification** — a wrong key or a
  tampered body fails the tag check, so `verified=True` is set ONLY when decryption succeeds.
- Parse the decrypted JSON: map the payment `result.code` (`000.000.*` = success) to
  `payment.succeeded` / `payment.failed` / `refund`, and carry `payload.id` as `provider_id`.
- Uses `cryptography`'s `AESGCM` (already a dependency). No key configured → `verified=False` with a
  reason, never a silent pass — the one outcome a payment webhook must never have.

### 49.5 — fix refund currency precision — BUG

`HyperPay.refund` formats the amount with `major(amount_minor)` — no currency — which defaults to SAR
(2-dp). A partial refund in a 3-decimal currency (KWD/BHD/OMR) is then off by 10×. The `refund` port
signature carries no `currency` at all, so no adapter CAN get it right.

- Add `currency` to the `refund` port method and thread it from the caller (the invoice's currency)
  through every adapter, so `major(amount_minor, currency)` uses the real exponent. Full refunds
  (`amount_minor=None`) are unaffected; the fix targets partial refunds in 3-dp currencies.

## Definition of Done

- Provider switch is per-tenant and runtime (`/operator/config`), audited, fail-closed on an
  unconfigured driver; the read is wired (no inert knob).
- `prepare_checkout` exists on the port and both server-session adapters; the customer route returns a
  session and NEVER a key; an unsupported provider says so; completion reconciles through `_mark_paid`.
- Auto-reactivate fires once, from every paid path, only `paused → active`, behind a runtime knob.
- The HyperPay webhook verifies by GCM auth tag and maps result codes; no key ⇒ `verified=False` with
  a reason; a planted-bad-tag test proves it refuses.
- Refund currency is correct for 3-dp currencies; a test asserts the formatted amount.
- Non-functionals: no unwired knob (both new RUNTIME_POLICY keys are read at use); egress hosts stay
  allow-listed; keys never cross an API boundary (`safety:no-secret-reads`); charge/refund/webhook
  stay off any scan path; gates + full suite green.

## Tasks

- [x] **49.4** Fix the HyperPay webhook: AES-256-GCM decrypt + verify (auth tag), map result codes,
  no-key ⇒ verified=False with a reason. Mutation-test: a tampered tag must be refused. *(Priority —
  it is a live security/reconciliation bug.)*
- [x] **49.5** Fix refund currency: add `currency` to the `refund` port + all adapters + callers;
  test the 3-dp case.
- [x] **49.1** Runtime per-tenant gateway driver: `RUNTIME_POLICY` + `rebuild_gateway`/resolver read
  the override, cache-keyed on `(tenant, driver, creds-gen)`, fail-closed on an unconfigured driver.
- [x] **49.3** Auto-reactivate on payment: hook `_mark_paid` (paused → active), runtime knob
  `BILLING_AUTO_REACTIVATE`, idempotent, event emitted; test every paid path.
- [x] **49.2** Checkout session: `prepare_checkout` port + HyperPay/Geidea adapters + customer route
  returning `{id, provider, redirect_url, widget}` (never a key), unsupported fallback, completion via
  `_mark_paid`.


---

**Phase 49 complete.** Two payment bugs fixed (HyperPay webhook now verifies via AES-256-GCM; refund carries the currency so 3-dp currencies are no longer refunded 10x) and three features shipped (runtime per-tenant gateway switch, fail-closed; auto-reactivate on payment from every paid path; customer checkout-session that never proxies a key). The payment layer stays general and plug-and-play, with one money truth in _mark_paid. billing 584 tests green.
