# WhatsApp Commerce + Payments — Integration Guide

**Audience:** a consuming platform (e.g. the Raje3 store-linking app) wiring shops onto this
infrastructure. **Status:** P0–P6 shipped. This is the end-to-end map of the primitives and the order
to call them in. We are **infrastructure, not a product** — this guide shows the pipes; your platform
owns the business logic, the shop's product data, and its self-serve UI.

Two services are involved and stay independent:
- **`notification-service`** — the WABA/Graph adapter home: catalog, product sync, product/order
  messages, inbound orders, and the **native** in-WhatsApp pay seam (build-ahead, KSA-dormant).
- **`payments-service`** — a secure isolated router (not a ledger) for the **external gateway
  pay-link**: each shop's sealed gateway credentials, a server-authoritative `PaymentRequest`, the
  public redirector, and webhook/reconcile.

**Conduit, never PSP.** For every payment path, funds move **customer → the shop's own gateway → the
shop**. Money never passes through us; we never hold funds or touch card data.

---

## 1. Onboard the shop (provisioning sequence)

Call these once per shop, in order. All `/internal/...` routes require the internal service token;
all `/api/v1/.../operator/...` routes require an operator JWT with `platform.config` (or
`platform.secrets` for credentials).

1. **Connect WhatsApp (Embedded Signup).** `POST /internal/whatsapp/embedded-signup` — exchanges
   Meta's code for the shop's business token and stores it as the tenant's WABA credential. (See the
   WhatsApp onboarding docs.) This is the prerequisite for everything below.

2. **Create + connect a catalog** (notification-service, P1):
   - `POST /internal/whatsapp/catalog` — create/get the catalog for the WABA.
   - connect the catalog to the WABA and set the phone number's commerce settings (same module).

3. **Sync products** (notification-service, P2): `POST /internal/whatsapp/catalog/products` with a
   batch of CREATE/UPDATE/DELETE items (`retailer_id`, name, price, currency, availability,
   image_url, description). Idempotent; batch-status is handled.

4. **Provision a payment option.** Pick per shop — the two are independent and independently lockable:

   **(a) External gateway pay-link** — *works in KSA today.*
   - Store the shop's gateway credentials (write-only): `POST /internal/payments/merchant-credentials`
     (or via the operator surface `PUT /api/v1/app/operator/secrets`). Sealed per-tenant under
     `SECRETS_KEK`; there is **no route that reads them back** — the panel gets a `hint()` only.
   - The option defaults **enabled**. To disable/lock it for a shop, see §3.

   **(b) Native in-WhatsApp pay** — *build-ahead, KSA-dormant.*
   - Requires the deployment flag `WHATSAPP_NATIVE_PAY_ENABLED=true` (off until Meta enables the
     gateway flow for the region) **and** the per-shop `native_pay` option turned on (see §3). Both
     gates must pass. The shop's `payment_configuration` name is registered on the WABA (Meta-side);
     pass it as `payment_configuration` on each order.

---

## 2. Run the commerce loop

- **Send a product / product list** (notification-service, P3):
  `POST /internal/notify/whatsapp/interactive` with `type: "product"` or `"product_list"` (≤30 items).
  Messages carry only IDs (`catalog_id` + `product_retailer_id`); Meta renders each item from the
  connected catalog. Recorded as a tenant-scoped Delivery like every other send.

- **Receive an order** (no new code): the durable inbound feed already records every inbound message.
  Read `GET /internal/whatsapp/inbound?changed_since=...`, filter `type == "order"`, and normalise
  with the `parse_order()` helper (quantity→int, item_price, line_total, order_total).

- **Take payment:**
  - **External pay-link:** `POST /internal/payments/requests` with `{tenant, reference, amount_minor,
    currency, return_url?}`. Idempotent by `(tenant, reference)`; returns a hosted `pay_url`. The
    amount is server-authoritative — a callback can only settle the recorded sum. The customer pays on
    the gateway's hosted page (redirector: `GET /api/v1/payments/pay/{id}`). Completion arrives via
    the shop's gateway webhook (`POST /api/v1/payments/webhook/{tenant}`, adapter-verified,
    idempotent) or a reconcile pull; either way one `payment.completed` / `payment.failed` event fires
    (no secrets or links in the payload). Confirm off that event.
  - **Native "Review and Pay"** (when enabled): `POST /internal/whatsapp/pay/order-details` with the
    order (items, subtotal, tax, shipping?, discount?, gateway, `payment_configuration`). The builder
    enforces `subtotal == sum(item*qty)` and `total = subtotal+tax+shipping-discount`. Send order
    updates with `POST /internal/whatsapp/pay/order-status`.
    - **The lookup is the source of truth.** A `type:"payment"` webhook is emitted as a
      `whatsapp.payment` event but is only a HINT — before acting on money, confirm with
      `POST /internal/whatsapp/pay/lookup`. Refund via `POST /internal/whatsapp/pay/refund`.

---

## 3. Per-shop option locks (enable / disable / lock)

Each service owns the options it enforces. An option resolves to its **built-in default** until an
operator sets an override.

| Service | Option | Default | Enforced at |
|---|---|---|---|
| payments-service | `external_paylink` | **on** | `POST /internal/payments/requests` (403 `DISABLED` when off) |
| notification-service | `native_pay` | **off** | `POST /internal/whatsapp/pay/*` (403 `NATIVE_PAY_NOT_ENABLED_FOR_TENANT` when off) |

Operator surface (per service):
- `GET  .../operator/options?tenant=<shop>` — every option with its effective `enabled` / `locked` /
  `source` (`default` | `override`). A self-serve UI reads `locked` to grey out a frozen toggle.
- `PUT  .../operator/options` — `{option, enabled, locked?, tenant?, force?}`. A **platform** row
  (`tenant` omitted) with `locked:true` is the fleet-wide hard override — it wins over any tenant row.
  A **tenant** row freezes just that shop.
- `DELETE .../operator/options/{option}?tenant=<shop>` — drop an override (back to default).

  payments-service prefix: `/api/v1/app/operator` · notification-service prefix:
  `/api/v1/notifications/operator`.

**Lock semantics:** enforcement reads only `enabled`. `locked` guards **writes** — changing or
clearing a locked scope is refused unless `force:true` is passed (a deliberate second step so a
careless re-toggle cannot undo a lock). Every toggle is audited (`operator.option_changed`).

---

## 4. What each side owns

- **You (the platform):** the shop's product catalogue data, prices, business rules, the self-serve
  UI, and the merchant/PSP relationship (SAMA/PSP licensing is the shop's and its gateway's concern).
- **This infrastructure:** the audited, rate-limited, tenant-scoped pipes — catalog/message plumbing,
  the sealed-credential store, the server-authoritative payment request + redirector + webhook, the
  native-pay builders and lookup/refund, and the per-shop option locks. No invoices, no ledger, no
  ZATCA — you keep your own books.
