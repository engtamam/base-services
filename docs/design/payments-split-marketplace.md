# Payments — Multi-Mode / Multi-Provider (Split-Payout Marketplace) Plan

**Status:** Phase 0 (spec + architecture; not started). **Owner:** Tamam. **Drafted:** 2026-10-07.
Consuming app example: a delivery/marketplace app (a separate team builds product/business logic on top).

This is a living plan — update the phase checkboxes as we ship. Each phase is its own release.
It extends, and does not replace, [`whatsapp-commerce-payments-plan.md`](./whatsapp-commerce-payments-plan.md).

---

## Goal

Grow `payments-service` from **one payment model** (the conduit pay-link) into an **extensible, multi-mode,
multi-provider** service, so a consuming platform can also take a payment and have it **split automatically**
between the platform's commission and a merchant's share — without the platform ever holding the money.

Concretely, support (eventually) two independently-selectable **models**:

1. **`conduit` (shipping today)** — each shop uses its OWN gateway credentials; funds flow
   customer → the shop's gateway → the shop. The platform mints a hosted pay-link and never touches money.
2. **`marketplace` (new, build-ahead; ships OFF)** — the PLATFORM holds one marketplace account at a
   licensed provider (Moyasar / Tap). Merchants are onboarded as **sub-merchants** of that provider.
   A payment is **split** at checkout (platform commission + merchant share); the **provider** holds the
   funds in its own escrow and pays out to each sub-merchant's bank account on a settlement cycle.

---

## Guiding principles (apply every phase)

- **We are infrastructure, not a product.** Generic, provider-neutral primitives; the consuming platform
  wires them its way. We never model a shop's products, prices, carts, or business rules.
- **Infrastructure, not an imposed mode.** We expose every linking method as an opt-in capability and
  never force one. Each platform curates which modes/providers it offers (and may force a single one);
  each merchant then selects exactly one. A platform only selects what the chosen provider declares it
  supports; adding a provider is declaring its capabilities, not changing the structure.
- **Conduit AND marketplace keep money out of our hands.** In `conduit`, funds go to the shop's gateway.
  In `marketplace`, the **licensed provider** holds and splits the funds; merchants are sub-merchants
  **under the provider's licence**, onboarded and KYC'd by the provider. We are the technical router.
- **Regulatory posture is a design constraint, not an afterthought.** The moment the platform holds,
  routes, schedules, or stores merchant balances itself, it enters SAMA licensing territory
  (Payment Facilitator / Aggregator / EMI). The `marketplace` model is designed specifically so the
  provider — not us — holds the funds. **We never build a "platform collects then pays out" mode.**
  (Not legal advice — the consuming platform confirms its own licence posture with a KSA regulatory
  adviser and in its provider contract.)
- **Zero behaviour change in Phase 0.** The structural refactor must leave every existing route, status,
  and guarantee byte-for-byte identical: all tests green, OpenAPI `--check` matches, all fleet gates green.
- **Inherit every existing security rail** for any new surface: write-only sealed credentials, SSRF base
  validation (write + read), open-redirect allow-listing, `safety:no-secret-reads`, write-ahead audit on
  every money/credential action, server-authoritative amounts, and idempotency on every money transition.
- **Every phase:** deep regression tests, all fleet CI gates green, path-scoped commits, its own release.
  Stay on v0.25 (patch bumps only). Service stays 100% self-contained (no shared cross-service lib).

---

## Current architecture (studied 2026-10-07 — the baseline we extend)

Facts established by reading the service end to end. Each is load-bearing for the plan.

- **The port is conduit-shaped.** `app/ports/payment_gateway.py` defines `PaymentGateway`
  (`@runtime_checkable` Protocol) with exactly three ops: `prepare_checkout`, `verify_webhook`,
  `fetch_status`, plus dataclasses `CheckoutSession` / `PaymentOutcome`. No onboarding, no split, no payout.
- **Adapter registry.** `app/adapters/payment/__init__.py` maps a driver string → adapter class
  (`console`, `null`, `tuwaiqpay`); `SELECTABLE_DRIVERS` excludes `null`; `build_gateway(driver, cfg)`
  fails closed to `NullGateway`. Adapters carry a `driver` attribute only — **no capability/model metadata**.
- **The gateway is resolved PER-REQUEST, not at boot.** Unlike every other port (wired in `container.py`),
  the payment gateway is built per operation by `MerchantGateways.gateway_for(tenant)` from the tenant's
  sealed creds. ⟹ New models/adapters touch `payments_deps` + `merchant_credentials`, **not** boot wiring.
- **Credential ownership is the real fork, and it is cleanly isolated.** `app/core/merchant_credentials.py`
  seals per-tenant `MERCHANT_*` secrets in their own collection/salt, with `platform_fallback=False` and
  `require_tenant=True` — it **forbids** a platform-scoped ("") credential by design. The `marketplace`
  model needs PLATFORM-scoped provider keys, which this store deliberately rejects. ⟹ **Do not weaken
  this store. Add a SEPARATE platform-marketplace credential store/resolver.** (Single most important call.)
- **"Mode" has a natural home: `OptionGate`.** `app/core/option_gate.py` holds per-tenant/per-platform
  enable+lock toggles with platform-lock-wins precedence, already enforced in `create_link` via
  `is_enabled("external_paylink")`. Build-ahead options ship **OFF** by default (the "native pay" precedent).
  ⟹ A new model is a new option (e.g. `marketplace_split`), shipped OFF, lockable fleet-wide.
- **`PaymentRequest` is checkout-shaped and server-authoritative.** `app/core/payment_requests.py` records
  `amount_minor`/`currency`/`provider_ref` with an atomic `(tenant, reference)` claim and a version-pinned
  terminal CAS (one completion, one event; success may supersede a prior non-success). A webhook can only
  settle the recorded amount. ⟹ Split needs an extension recording the **destinations + fee**, with the
  same discipline: **the split must sum to the recorded amount (fail-closed)**, verified on apply.
- **Orchestration.** `app/core/payments.py` (`PaymentService`) mints/re-uses a link, redirects
  (open-redirect-safe allow-list), and applies outcomes idempotently from webhook OR reconcile (one seam).
- **Events are not centralised.** No `events.py` — just `TOPIC_COMPLETED = "payment.completed"` /
  `TOPIC_FAILED = "payment.failed"` string constants in `payments.py`, emitted on the bus.
  ⟹ Minor cleanup opportunity before adding payout/onboarding events.
- **Routes.** Internal (`/internal/payments/*`: create/get/reconcile + write-only merchant-creds),
  public (`/api/v1/payments/pay/{id}` redirector + `/api/v1/payments/webhook/{tenant}`), operator
  (`/api/v1/app/operator/*`: secrets/config/options). All inherit the hardening rails above.
- **Reconcile sweep.** `app/core/reconcile_sweep.py`: paged (keyset cursor, never `scan_all`), tenant-safe,
  idempotent, per-row isolated, bounded concurrency, cancellable; OFF by default. ⟹ Reusable shape for
  **payout reconciliation**.
- **Boot guards are the extensibility contract.** `config.py` fails fast on missing required secrets
  (`assert_secrets_config_ok`, etc.), and `CONSENT_GATED` shows the pattern: a declared flag gates a guard.
  ⟹ New marketplace secrets get a fail-fast guard **conditioned on the marketplace model being enabled**,
  so the dormant mode never breaks boot.
- **Tests** live under `tests/{unit,integration,contracts,security}`; per-service venv
  `.venv/Scripts/python.exe -m pytest tests -q`.

---

## Target architecture (where we are going)

### Capabilities and models

- **Capabilities** (what an adapter can do): `checkout`, `onboarding`, `split`, `payout`.
- **Payment models** (how a payment is structured): `conduit`, `marketplace`.
- **Onboarding modes** (how a merchant's KYB data reaches the provider), declared per provider:
  - **`api`** (mode A) — WE collect the merchant's data in our UI and forward it via the provider's
    onboarding API; store only the returned destination/sub-merchant id.
  - **`hosted`** (mode B) — the merchant submits data + KYC **directly to the provider** (e.g. Tap's
    JS Connect, embedded in our UI); we never touch the documents, only receive the id/status. Lighter
    liability — prefer where available.
  - In **both**, the **provider does the KYC/KYB and verifies** (it is the licensed Payment
    Institution); we never verify eligibility ourselves.
- Each **adapter/provider declares** its payment model(s), capability set, AND supported onboarding
  modes. Today: `tuwaiqpay`/`console` → `model=conduit, capabilities={checkout}`. Future:
  `tap` → `model=marketplace, capabilities={onboarding, split, payout}, onboarding={api, hosted}`;
  `moyasar` → `model=marketplace, …, onboarding={api}` (hosted TBC with the provider).
- **A platform may only select what the chosen provider declares.** An unsupported selection (a mode
  or onboarding method the provider does not offer) is **refused clearly**, never silently downgraded.
  Adding a new provider later = declaring its capabilities; the structure does not change.
- **Capability detection stays cheap:** keep `PaymentGateway` (checkout) as a `@runtime_checkable`
  Protocol; add a second `MarketplaceGateway` Protocol. The service checks the object's capabilities /
  `isinstance` before use and refuses clearly if unsupported — no heavy registry.

### Mode selection — single-select per tenant (infrastructure, not an imposed mode)

- **We impose no mode.** We expose the available modes as a menu; each **platform curates which modes
  it offers**, and may **force a single mode** (a one-item menu) if it wants.
- **Each merchant (tenant) selects EXACTLY ONE active payment mode** from the platform's menu — a
  radio/select, not independent toggles. Two modes are never active for one tenant at once; a payment
  is never split across two models. Setting a new mode **replaces** the old (exactly-one), so no
  merchant is ever left with two live credential sets.
- This needs a small **per-tenant mode-selection primitive** (one value from an allowed set + a
  platform lock), distinct from `OptionGate`'s independent booleans. `create_link` reads the tenant's
  selected mode and resolves that model's gateway — no per-transaction ambiguity.
- The platform controls the menu and can **lock** a tenant's selection (same precedence as
  `OptionGate`: a platform lock wins over any tenant choice).

### Credential resolution seam (the critical fork)

- Introduce a small `CredentialResolver` seam: "how are the creds for THIS model resolved?"
  - `conduit` → the existing per-tenant `MERCHANT_*` store (unchanged, still `require_tenant=True`).
  - `marketplace` → a NEW platform-scoped store (its own collection + salt), holding the platform's single
    provider marketplace keys; plus a per-tenant **sub-merchant mapping** (`tenant → provider destination id`).
- **We store the provider's destination/sub-merchant id, never the merchant's raw IBAN** (the provider
  holds the bank details — minimises our liability).

### Split record (money-correct by construction)

- Extend `PaymentRequest` (or a sibling record) to carry the server-authoritative **split**: destinations
  (sub-merchant id + amount) + platform fee, with the invariant **sum(destinations) + fee == amount_minor**
  (fail-closed; refuse the create otherwise). Optionally a per-destination **payout state** reconciled like
  the existing request state.

### New events (centralised)

- A single topics module: existing `payment.completed` / `payment.failed`, plus (Phase 1+)
  `merchant.onboarding_updated`, `payout.settled`, `payout.failed`. Consumers subscribe the normal way.

### Provider landscape (verified 2026-10-07 — re-verify at build)

Both candidates are **SAMA-licensed Payment Institutions** that hold the funds — which is what keeps
the platform out of the fund-holding licence (confirm your own split role in the contract + with a
KSA adviser):

| | Tap (Tap Financial) | Moyasar (Moyasar Financial) |
|---|---|---|
| SAMA licence | ✅ (to 2029-09-04) + KW/UAE | ✅ (to 2030-01-13) |
| Marketplace / split / sub-merchant | ✅ | ✅ |
| Onboarding `api` (mode A) | ✅ Business/Files/Merchant APIs | ✅ KYB flow |
| Onboarding `hosted` (mode B) | ✅ **JS Connect** (merchant → Tap direct, embedded) | ❓ E-KYC flows exist; hosted-direct **TBC with provider** |
| Opening the marketplace account | Platform applies via sales/contract; merchants added as sub-merchants | Register → sales contact; merchants as sub-merchants |
| Fees | ~2.75% local / ~3.25% intl + FX; **no monthly**; no per-merchant setup | ~1.5% mada / ~2.2–2.5% cards + ~1 SAR; settlement Mon/Thu; **marketplace pricing via sales** |

- **No per-merchant setup/monthly in the marketplace model** — sub-merchants are onboarded under the
  platform's one account; cost is per-transaction on the split. Marketplace pricing is negotiated by
  volume (both moved public rate cards to "contact sales").
- **Phase 1 leans Tap** (A + B via JS Connect — the lower-liability onboarding). Moyasar as the Phase 2
  second provider (confirm hosted onboarding before relying on mode B for it).

---

## Build breakdown — internal / regulatory / provider-doc (with security + Python per step)

Security best practices below are from a 2026 best-practice pass (see References); **most are already
proven in the conduit code — we reuse them, not reinvent.** Everything in the provider-doc column is
re-verified against the provider's first-party docs at build time, with a dated note saved under
`docs/integrations/` (the fleet rule).

### A. Internal steps (we build and own)

| # | Step | Security best practice | Python leverage |
|---|---|---|---|
| 1 | Capability / mode / onboarding taxonomy + declaration | Fail-closed: an unsupported capability/mode is refused clearly, never downgraded | `@runtime_checkable` Protocols, `Enum`/`Literal`, frozen dataclasses |
| 2 | Mode-selection primitive (single-select + lock) | Exactly-one; platform lock wins; refuse a mode the provider does not support | `Literal` validation (pydantic), CAS store |
| 3 | Platform-scoped marketplace secret store (separate from `MERCHANT_*`) | **Envelope encryption**, per-scope crypto isolation, write-only, never logged, 90-day rotation + overlap window | `cryptography` AES-256-GCM (existing `secret_store`), own salt/collection |
| 4 | Sub-merchant mapping store (`tenant → destination id`) | **PII minimisation** — store only the provider id, never raw IBAN/documents | pydantic validation, tenant-scoped keys |
| 5 | Split record + validation | **Server computes the split, never the browser**; `sum(destinations)+fee == total` fail-closed | `int` minor-units + `Decimal` quantize (never `float`) |
| 6 | Webhook verify (payout/onboarding) + idempotent apply | **HMAC-SHA256 over the RAW body**, `hmac.compare_digest` (timing-safe), **300s** timestamp tolerance (OWASP), dedup on `event_id`, dual-secret during rotation, reject-on-missing (no "log & continue"); unsigned provider → verify-by-fetch | `hmac.compare_digest`, `hashlib`, pydantic payloads |
| 7 | Idempotency on every money op | Client key + atomic CAS claim (pending → done), handler idempotent | existing `_claim`/`_finalize` pattern |
| 8 | Payout reconciliation sweep | Paged (keyset cursor), per-row isolated, idempotent, OFF by default | `asyncio` + semaphore (existing `reconcile_sweep` shape) |
| 9 | SSRF egress guard on the provider base | https + allowlist, validated at write **and** read | existing `validate_gateway_base` |
| 10 | Audit write-ahead on credential / onboarding / split / payout | An action that cannot be recorded does not happen (critical) | existing auditor |
| 11 | Boot guards conditioned on marketplace being enabled | Fail-fast on a missing secret, gated by a declared flag (the `CONSENT_GATED` pattern) | pydantic-settings validators |
| 12 | Centralise event topics | One declared registry; an undeclared event reaches nobody | module-level constants + `EMITTED` |

### B. Regulatory steps (decisions, not code — platform + adviser)

1. **Decide the fund-holding posture** — the provider holds (merchants are sub-merchants under its
   licence), which keeps the platform out of the fund-holding licence. **Get a KSA regulatory adviser's
   sign-off.**
2. **Provider contract** — it must state the merchants are sub-merchants under the provider's licence;
   nail down the split role, fees, and settlement terms.
3. **PCI DSS (v4.0.1, 2026)** — hosted pages/onboarding → **SAQ A** (~90% scope reduction); we never
   touch PAN; prefer hosted onboarding (mode B) to minimise scope.
4. **Data protection (Saudi PDPL)** — minimise any PII routed in mode A; KYC/KYB is the provider's, not
   ours.
5. **Open the marketplace account** — platform-level application/contract (sales, not self-serve).

### C. Provider-doc-dependent steps (finalised at build against first-party docs)

1. Exact onboarding field set / embed (Tap: Business/Files/Merchant APIs, or JS Connect for mode B).
2. Split request shape (the `destinations` object + fee).
3. Webhook signature scheme (does the provider sign? header name / algorithm / secret — else verify-by-fetch).
4. Payout status / settlement-cycle API (`fetch_payout_status`).
5. Onboarding-status callbacks (the source of `merchant.onboarding_updated`).
6. Currency/units, error codes, auth scheme (token TTL).
7. Hosted onboarding (mode B) availability per provider (Tap confirmed; Moyasar TBC).

---

## Phases

Legend: ☐ not started · ◐ in progress · ☑ shipped.

### ☑ Phase 0 — Make it extensible (ZERO behaviour change)

Pure structural prep. No new routes, no new provider, no new model active. Proof of "no behaviour change":
full suite green + OpenAPI `--check` identical + `run_all_gates.sh --strict --profile aggregate` green.

- [x] **Taxonomy constants** — define `capabilities` (`checkout/onboarding/split/payout`), payment
      `models` (`conduit/marketplace`), and `onboarding` modes (`api/hosted`) in the port module.
- [x] **Adapters declare model + capabilities + onboarding modes** — add the declaration to `tuwaiqpay`,
      `console`, `null` (all `conduit` / `{checkout}` / onboarding n/a; `null` declares none). Registry
      carries the metadata.
- [x] **Capability assertion** — `PaymentService` asserts the resolved gateway supports the capability it is
      about to use (today implicitly `checkout`); behaviour for `conduit` stays byte-identical.
- [x] **CredentialResolver seam** — extract "resolve creds for a model" behind one interface with a single
      implementation = today's per-tenant `MerchantGateways` path. **No change to `MERCHANT_*` isolation.**
- [~] **Mode-selection seam (single-select)** — DEFERRED to Phase 1 (it only gets a real enforcement reader once `marketplace` exists as a second mode; adding it now would be an unread knob). ORIGINAL NOTE: — introduce the per-tenant "selected payment mode" primitive
      (one value from an allowed set + platform lock), with exactly-one semantics. One implementation, one
      allowed value (`conduit`) now, so behaviour is unchanged; the platform-curated menu + force-one + lock
      are wired but trivial while only one mode exists.
- [x] **Centralise event topics** — move `payment.completed`/`payment.failed` into one small module;
      re-export so nothing else changes.
- [x] **Document the model as a (dormant) option** — register the taxonomy / note `marketplace` as a future
      selectable mode (not added as active yet) and the conditional-guard pattern for its secrets.
- [x] **Tests** — assert each adapter's declared model/capabilities/onboarding modes; the resolver seam and
      the mode selector return exactly today's result; the capability assertion refuses an unsupported
      capability; an unsupported mode/onboarding selection is refused clearly. All existing tests green.

**DoD:** zero behaviour change (proven as above); no dead code (defer `MarketplaceGateway` Protocol to
Phase 1 with its first implementer); provider-neutral; English; self-contained; one path-scoped commit +
release (v0.25 patch).

- [x] Capability assertion is **fail-closed** — a gateway that does not declare `checkout` is refused
      before any gateway call (no half-supported op).
- [x] Declaration coercion is hostile-input-proof — malformed/stringly-typed/non-iterable metadata
      **never invents a capability** (only ever refuses more); taxonomy sets are immutable frozensets.
- [x] A refused create **releases the (tenant, reference) claim** (no DoS on an order id) and **persists
      no PaymentRequest** (no half-made order a webhook could settle).
- [x] `describe_drivers` leaks **no secret/credential/base**, never lists `null`, is deterministic.
- [x] `CredentialResolver` conformance is strict — a partial object is rejected (`isinstance` false).
- [x] Event topic catalogue (`EMITTED`) is an immutable tuple; re-export unchanged.
- [x] Attacker-mindset security tests **+** bug/edge tests written; full service suite + all fleet gates
      green; OpenAPI `--check` unchanged.
### ◐ Phase 1 — Marketplace capability + first provider adapter (code-complete; release held)

Code-complete on the phase branch (commits 1a/1b/1c/1d); NO release yet (per the hold-all-releases
directive). 106 Phase 1 tests; full suite + 55 fleet gates green; OpenAPI regenerated.

- [x] **`MarketplaceGateway` Protocol** — `prepare_split_checkout()`, `verify_webhook()`, `fetch_status()`,
      `onboard_merchant()`, `fetch_payout_status()` + SplitDestination/OnboardResult/PayoutStatus.
- [x] **Platform-scoped marketplace credential store** — separate collection + salt; write-only; a
      write route (`/internal/payments/marketplace-credentials`). (No separate boot flag: marketplace is
      "enabled" iff provider+key are set — `configured()` fail-closed — so there is no unread flag.)
- [x] **Sub-merchant mapping store** — `tenant → provider destination id` (+ onboarding status), ACTIVE-
      gated. `api` onboarding forwards KYB and stores only the returned id; `hosted` returns a handle and
      the destination lands via `/sub-merchants/destination`. Never stores the IBAN/documents.
- [x] **First adapter — Tap** — split charge (destinations in major units), verify-by-fetch + timing-safe
      hashstring, onboarding (api/hosted), payout status; dated reference in `docs/integrations/`.
      Declares `model=marketplace, capabilities={checkout, onboarding, split, payout}, onboarding={api, hosted}`.
- [x] **Split record + validation** — server-authoritative `plan_split`; `sum(shares) ≤ total`, platform
      fee = remainder; fail-closed on every hostile shape.
- [x] **Mode-selection primitive (single-select)** — `PaymentModeGate` + `/internal/payments/payment-mode`;
      `create_link` resolves the mode and routes conduit vs marketplace; conduit default = unchanged.
- [x] **`marketplace` as a selectable mode** — selectable only where configured; platform force/lock; OFF
      until a platform sets a provider + a tenant's mode.
- [~] **Events** — `merchant.onboarding_updated` shipped; `payout.settled`/`payout.failed` move to Phase 2
      with the payout reconcile sweep that emits them.
- [x] **Deep tests** — onboarding happy/invalid, split sums, split-checkout mint, webhook verify +
      forged/unsigned/unreachable, capability + mode refusals, cross-tenant isolation, OFF-by-default.

**DoD (plan PAST the checklist):** onboarding/split/webhook are hot paths — no blocking I/O on the loop
(mirror `asyncio.to_thread` offload used for the gateway HTTP); no unwired config knob (every new setting is
read by code and guarded); non-functionals in scope: SSRF-safe provider base, write-only creds, audit
write-ahead on onboarding + split-create + payout settle, bounded/paged payout reconcile. OpenAPI regen +
check. One release.

**🔒 Security hardening — attacker-mindset, CHECK each (≥50 tests, all green):**
- [x] **Webhook verification**: trust anchor = **verify-by-fetch** (re-fetch the charge); a present-but-wrong
      hashstring is refused `hmac.compare_digest` timing-safe WITHOUT a fetch; an unsigned webhook verifies
      by fetch; replay is absorbed by the idempotent `apply_outcome` CAS. Tested forged / unsigned /
      unreachable. (Tap's hashstring field order/header + a timestamp-tolerance window stay CONFIRM-at-build;
      verify-by-fetch is the stronger anchor meanwhile.)
- [x] **Split integrity**: server computes the split; `sum(shares) ≤ total`, fee = remainder, **fail-closed**;
      hostile shapes (dupe/negative/bool/non-int/over-total) refused → nothing minted. Tested.
- [x] **Secrets**: platform-marketplace keys in a **separate sealed store** (own salt = crypto isolation),
      **write-only** (hint only), envelope-encrypted, rotation+overlap. Tested: no route returns a value, a
      relocated merchant blob fails the GCM tag.
- [x] **SSRF**: provider base is https + non-private, validated at **write and read**. Tested with
      non-https / private / loopback IP-literal bases.
- [x] **PII minimisation**: store only the provider destination id, **never raw IBAN/documents**; the
      onboarding event carries no KYB/PII. Tested.
- [x] **Idempotency**: create is claim-guarded; the marketplace webhook settles through the same idempotent
      `apply_outcome` CAS as conduit (one completion, replay-safe). Tested.
- [x] **Audit**: write-ahead on marketplace-credential set/clear, sub-merchant onboard/destination, mode
      set; split-create via `request_created`. (Payout-settle audit lands with Phase 2 payout reconcile.)
- [x] Attacker-mindset security tests **+** bug/edge tests (106 Phase 1 tests); full suite + 55 fleet gates
      green; OpenAPI checked.

### ☐ Phase 2 — Second provider + payout reconciliation + operator surface

- [ ] **Second adapter — Moyasar** (the one not done in Phase 1) — proves provider-neutrality. Confirm
      whether it offers `hosted` onboarding (mode B) before declaring it; declare `onboarding={api}` only
      until confirmed.
- [ ] **Payout reconciliation sweep** — reuse the `reconcile_sweep` shape for missed payout webhooks
      (paged, idempotent, per-row isolated, OFF by default).
- [ ] **Operator surface** — manage the marketplace option + (platform) marketplace credentials + view
      sub-merchant onboarding status, all audited; no secret reads.
- [ ] **Docs** — consuming-platform integration guide: onboarding fields per provider, split request shape,
      settlement/payout cycle, and the regulatory decision table (who holds funds → licence).

**DoD:** both providers pass the same capability tests; payout reconcile proven idempotent; operator routes
audited; OpenAPI in sync; release.

**🔒 Security hardening — attacker-mindset, CHECK each (≥50 tests, all green):**
- [ ] **Payout reconcile** is idempotent and **bounded/paged** — no double-pay on a replayed/missed
      webhook, no unbounded scan a caller could weaponise (DoS). Tested with a replayed payout callback.
- [ ] **Operator surface**: authz-capability-gated, **no secret reads** (`safety:no-secret-reads`),
      every mutation audited write-ahead. Tested that a non-privileged caller is refused and no value leaks.
- [ ] **Second provider** passes the SAME adversarial battery (forged/replayed/tampered webhook, tampered
      split, SSRF base, secret-read attempt) — provider-neutral security, not provider-specific.
- [ ] **Cross-tenant isolation**: a tenant can never resolve, pay out to, or onboard under another
      tenant's sub-merchant/destination. Tested with a forged/mismatched tenant.
- [ ] Attacker-mindset security tests **+** bug/edge tests; full suite + fleet gates green; OpenAPI checked.

---

## Explicitly OUT of scope (do not build)

- **A "platform collects then pays out" mode** — this is exactly what triggers SAMA licensing; the whole
  marketplace design exists to avoid it.
- **Holding a withdrawable merchant balance in our wallet** — `wallet-service` stays a ledger/accounting
  primitive; it is not a payout engine. (The wallet's `destination` field may hold a provider destination id
  for reporting, but the wallet never moves money out.)
- **Storing merchants' raw bank details (IBAN)** where avoidable — the provider owns KYC + bank data.
- **Any consuming-platform-specific flow** (a specific app's delivery/commission logic) — we ship primitives.

---

## Open questions (resolve before/within Phase 1)

- **Provider choice for Phase 1 — DECIDED: Tap** (supports both onboarding modes via JS Connect = mode B,
  the lower-liability path; marketplace split + sub-merchant leads; SAMA-licensed). Moyasar is Phase 2.
  Still confirm fees/settlement terms in the Tap contract. (Both confirmed split + sub-merchant, 2026-10-07.)
- **Confirm Moyasar hosted onboarding (mode B)** with the provider before declaring `onboarding={hosted}`
  for it — its public material confirms E-KYC/KYB flows but not a merchant-direct hosted link.
- **Exact onboarding field set per provider** — confirm against first-party docs at build time; both validate
  KSA data (CR, National ID/Iqama, IBAN, brand name) via third parties even in test.
- **Does any consumer need per-tenant provider override** in marketplace mode, or is the platform account
  always the single marketplace? (Affects whether the credential resolver needs a per-tenant branch.)
- **Centralised topics module** — confirm no other service imports the current `TOPIC_*` constants by path
  before moving them (keep a re-export if so).

---

## References

- Current service: `services/payments-service/` (studied 2026-10-07).
- Related: [`whatsapp-commerce-payments-plan.md`](./whatsapp-commerce-payments-plan.md),
  `docs/integrations/tuwaiqpay-api-reference.md`.
- Regulatory (2026-10-07 web research, informational — confirm with a KSA adviser):
  SAMA PSP licensing categories (Payment Gateway / Facilitator Cat B / Aggregator Cat A / EMI);
  split/sub-merchant models keep the platform out of fund-holding when the provider holds the funds.
