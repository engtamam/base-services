# WhatsApp Commerce + Payments — Infrastructure Plan

**Status:** Phase 0 (spec + architecture locked). **Owner:** Tamam. **Started:** 2026-09-16.
Consuming app: Raje3 store-linking app (a separate team builds product/business logic on top).

This is a living plan — update the phase checkboxes as we ship. Each phase is its own release.

---

## Goal

Give any consuming platform **plug-and-play infrastructure** to:
1. Manage a WhatsApp **catalog** per connected shop (WABA) via the API (not manual).
2. **Sync products** in batch (create/update/delete).
3. Send product/order **messages** and receive customer **orders**.
4. Take **payment**, two independently-lockable options per shop:
   - **External gateway pay-link** — the shop's OWN gateway (TuwaiqPay etc.); works in KSA today.
   - **Native in-WhatsApp pay** (`order_details` "Review and Pay") — build-ahead seam, dormant in KSA.

## Guiding principles (apply every phase)

- **We are infrastructure, not a product.** Generic primitives; the platform wires them its way. We never model a shop's products, prices, or business rules.
- **Conduit, never PSP.** For payments, funds flow **customer → the shop's own gateway → the shop**. Money never passes through us. We never hold funds and never touch card data (PCI SAQ-A: the card stays on the gateway's hosted page).
- **The shop owns merchant/compliance.** SAMA/PSP licensing is the shop's and its licensed gateway's concern — not ours. We provide the pipe.
- **Verify Meta shapes against current first-party docs at build time**, with a dated note (per the fleet rule). Meta's commerce/payments surface changes by region and version.
- **Every phase:** deep tests, all fleet CI gates green, path-scoped commits, its own release.

---

## Verified Meta facts (2026-09-16 — re-verify each at build time)

- **Native payment via payment gateway** (`order_details` interactive, `action.name:"review_and_pay"`): **India only** today (INR; India beneficiary/importer fields; gateways BillDesk/Razorpay/PayU/Zaakpay). Brazil is a separate flow. **Not usable in KSA.**
  - Known shape (for the build-ahead seam): `action.parameters` = stringified JSON with `reference_id` (≤35 chars), `type` (digital/physical-goods), `payment_settings[]`, `currency`, `total_amount{value, offset}`, `order{items, subtotal, tax, shipping?, discount?, status:"pending"}`.
  - `order_status` message (`review_order`): status ∈ pending/processing/partially-shipped/shipped/completed/canceled (completed & canceled terminal).
  - Payment webhook `type:"payment"` (status captured/pending). **Do not trust the webhook alone** — verify via lookup `GET /{phone_number_id}/payments/{payment_configuration}/{reference_id}`. Refund `POST /{phone_number_id}/payments_refund`.
- **WhatsApp Pay (consumer wallet):** live only in India/Brazil/Singapore; **expected in KSA during 2026**. Until then, KSA checkout = external hosted link in-thread.
- **Catalog/Commerce API:** catalog is created on the business and connected to the WABA + the phone number's commerce settings; products are managed in **batch**. Exact endpoints/fields (`/owned_product_catalogs`, `/{catalog_id}/items_batch` with `requests:[{method, retailer_id, data:{...}}]`, `/{phone_number_id}/whatsapp_commerce_settings`) are the community-documented shape — **confirm against Meta's Marketing/Commerce API reference in Phase 1/2** before building each call.
- **Limits:** standard catalog ~500 products; multi-product message ≤30 items; cart ≤99 units/item.

## Existing infrastructure we reuse

- `notification-service`: the Meta/WABA adapter home (`adapters/whatsapp/meta.py`, media upload, Embedded Signup, `provider_pool`, per-tenant WABA creds), plus `url_button_param` (shipped v0.24.4) to deliver a pay-link as a template URL button.
- `PaymentGateway` port (billing-service): `charge/refund/verify_webhook`, plus `prepare_checkout`/`fetch_status` on some adapters; `CheckoutSession{redirect_url, widget}`, `Charge`, `WebhookEvent{event_id}` (dedupe). Adapters: tuwaiqpay, moyasar, checkout, geidea, hyperpay, aps. **We reuse the SHAPES**, under a separate `MERCHANT_*` credential namespace — we do NOT reuse billing's `_mark_paid`/Invoice/Ledger/ZATCA.
- Billing pay-link pattern (token→checkout_url, public redirector, TTL, allowed-hosts) — the model for the conduit's link + redirector.
- `core/secret_store.py`: AES-256-GCM sealing per value under `SECRETS_KEK` (never leaves env), write-only API, `hint()` only, rotation window. **The base for `MERCHANT_*` credential storage** (extend to per-tenant isolation below).

---

## Architecture decisions (locked)

- **Catalog + products + product/order messages → `notification-service`** (the WABA/Graph adapter home). New adapters, no new service.
- **Payments → a NEW, lightweight `payments-service`** built from `_template`. It is a **secure isolated router, not a ledger**: it holds only (a) each shop's sealed gateway credentials, (b) a `PaymentRequest` (server-authoritative amount + TTL + idempotent outcome), (c) the public redirector, (d) webhook verify/reconcile, (e) a `payment.completed` event. No invoices, no ledger, no ZATCA — the consumer keeps its own books.
  - *Why its own service, not an adapter in notification:* credential isolation (payment secrets never share a process/store with messaging secrets), a clean money-webhook trust boundary, and independent blast radius.

### Credential isolation model (`MERCHANT_*`) — highest security

Best practice (envelope encryption + per-tenant cryptographic isolation, AWS/WorkOS 2025): a long-lived KEK (in env/HSM, never exported) wraps a per-object DEK; each tenant is cryptographically isolated so one tenant's compromise never exposes another's.

Our implementation:
- Reuse `secret_store`'s AES-256-GCM-under-`SECRETS_KEK` sealing, in a **separate `MERCHANT_*` namespace/collection** distinct from platform `PAYMENT_*`.
- **Per-tenant isolation:** key/collection namespaced by tenant (length-prefixed), and every read is tenant-guarded — a mismatched tenant reads as absent (IDOR guard), never another shop's secret.
- **Write-only at the API:** a shop enters its gateway key/secret; there is **no route that ever returns it**; the panel gets only `hint()`. Used in-process only to build a checkout.
- **Never logged, never echoed:** the secret never appears in a log, an error, a webhook, or a response. Redaction verified by test.
- **Rotation window** (existing) so an in-flight checkout survives a key change.
- KMS-backed KEK is a config seam (`SECRETS_KEK` can be `kms:*`) for deployments that want an HSM — no code change.

---

## Phases (each = its own release)

- [x] **P0 — Spec + architecture (this doc).** Verify Meta facts, lock homes + credential model. *No behavior.*
- [x] **P1 — Catalog adapter (notification-service).** Create/get catalog for a WABA; connect catalog↔WABA; set phone-number commerce settings. Internal routes per tenant; per-tenant WABA creds. Verify catalog endpoints vs Meta ref first.
- [x] **P2 — Product batch sync (notification-service).** `items_batch` CREATE/UPDATE/DELETE; product fields (retailer_id/name/price/currency/availability/image_url/description); idempotent; async batch-status handling; list/read.
- [x] **P3 — Product & order messaging (notification-service).** `build_product` (Single Product) + `build_product_list` (Multi-Product, ≤30 items) in `core/whatsapp_interactive`, exposed via `POST /internal/notify/whatsapp/interactive` (`type: "product" | "product_list"`) through the same audited/rate-limited/ceiling-aware Delivery plumbing as every other send. Messages carry only IDS (catalog_id + product_retailer_id) — Meta renders each item from the connected catalog. **Inbound orders need NO new code:** the existing `wa_inbound` durable feed already records every inbound message whole (type + full `message`), so an `order` message (`type:"order"`, `order{catalog_id,text,product_items[]}`) rides it — the consumer reads `changed_since`, filters `type=="order"`, and uses the new pure `parse_order()` helper (normalises quantity→int, item_price, line_total, order_total). Meta shapes re-verified Sep 2026. 662 tests green. *(Committed, not released — releases held until all commerce phases done.)*
- [x] **P4 — Payments conduit: external pay-link (`payments-service`).** ⭐ *Works in KSA today.* New self-contained service scaffolded from `_template` (port 8024). Credential isolation researched online first (envelope encryption / per-tenant DEK / never-store-key-with-data / never-log / idempotency-on-event-id — confirms our sealed store). Built: **`MERCHANT_*` sealed per-tenant creds** in their OWN collection (`merchant_secrets`) under their OWN scrypt-derived key (distinct salt), write-only at the API (set/clear/hint, no read route), no cross-tenant fallback (fail-closed to null gateway). **`PaymentGateway` port** + `tuwaiqpay` (verify-by-fetch, unsigned webhook), `console` (dev/test), `null` (fail-closed) adapters. **`PaymentRequest`** primitive: server-authoritative amount/currency (a callback can only settle the recorded sum; mismatch → refused+flagged, never completed), TTL, idempotent apply shared by webhook + reconcile via version-pinned CAS (once-only completion + one `payment.completed`/`payment.failed` event, no secrets/links in the payload), created via CAS insert-if-absent (added cas to this service's datastore port+memory+postgres with the aligned semantics from the identity incident, + cas contract tests). Public **redirector** (`GET /api/v1/payments/pay/{id}`: TTL, host-allowlist fail-closed, expires stale). Public **webhook** (`POST /api/v1/payments/webhook/{tenant}`: adapter-verified, idempotent, tenant-in-path can't cross-settle). Internal API: create link (idempotent by order reference), status, reconcile, write-only creds mgmt. Money-collections registered for backup (sealed). Money-grade tests: idempotency, **concurrency (racing webhooks → one completion)**, tenant isolation, **secret redaction (sealed row = ciphertext; no cred/link in events)**, amount-mismatch, fail-closed. 368 tests green. Per-tenant per-option LOCK hook (`is_enabled`) in place; lock STORE + wiring is P6. *(Committed, not released.)*
- [ ] **P5 — Native in-WhatsApp pay seam (build-ahead, KSA-dormant).** `order_details` "Review and Pay" builder (exact Meta shape) + payment_configuration mgmt + payment webhook (`type:payment`) + lookup-verify (never trust webhook alone) + refund. Region-gated + per-tenant lockable; dormant until Meta enables KSA — then flip config, no new dev.
- [ ] **P6 — Wiring, per-option locks, provisioning, docs.** Per-tenant per-option enable/lock; onboarding/provisioning of catalog + payment config; operator/admin surface; end-to-end doc; final gate sweep.

## Definition of done (per phase)

Deep tests (incl. security/isolation/idempotency for money paths) · all fleet CI gates green (backup-coverage, audit-wiring, atomic-cas, scan-discipline, pii, config-imports, dead-code, admin) · path-scoped commit(s) · its own release · Meta shapes verified against current first-party docs with a dated note.
