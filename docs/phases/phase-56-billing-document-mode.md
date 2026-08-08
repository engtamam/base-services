# Phase 56 — billing: document mode — our own receipt by default, tax invoice by choice

Not every platform charges tax at launch, and a ZATCA tax invoice it does not need is a compliance
burden it should not carry. So the money document a payment produces becomes an OPERATOR CHOICE, and
the safe default is the one every platform can issue: **our own internal receipt** — a payment
confirmation (سند قبض) with no VAT, no ZATCA, no seller VAT number — carrying the **gateway's
transaction reference** for reconciliation.

`BILLING_DOCUMENT_MODE` (runtime, per tenant): `receipt` (default) · `tax_invoice` · `both`.

## Why our own receipt, not the gateway's

Researching the official gateway docs settled it: **only Stripe (`charge.receipt_url`) and PayTabs
(`invoice_link`) expose a hosted receipt URL.** Tap / PayPal / Moyasar only *email* one; HyperPay /
Checkout.com / Geidea / Amazon Payment Services expose no receipt URL at all. A receipt that depends
on the gateway therefore works for two providers and fails for seven. OUR receipt works for all nine
— consistent brand, bilingual (AR/EN), and it carries the gateway's `provider` + `provider_id` so the
payment is still traceable to the acquirer. Surfacing a gateway hosted link stays an OPTIONAL extra
where one exists.

## What already exists (so we build on it, not beside it)

- **The transaction reference is already persisted and legally retained.** `Payment` stores
  `provider` + `provider_id` on every path (card, webhook, settlement), is searchable by
  `by_provider_id`, is backed up (`payments` collection), and billing's erasure "deletes nothing —
  everything here is a legal record" (it pseudonymises a buyer's name on the invoice and retains the
  figures; it never touches `payments`). `provider_id` is not PII — retaining it is correct.
- **The PDF renderer.** `reportlab_renderer` already produces the premium bilingual TAX invoice
  (VAT lines + ZATCA QR). A receipt is the same renderer in a simpler mode.
- **`_mark_paid` is the one place an invoice becomes paid.** ZATCA e-invoicing already funnels through
  it; the document decision belongs here too, once.

## The work

### 56.1 — `BILLING_DOCUMENT_MODE`, the operator choice
- `BILLING_DOCUMENT_MODE: str = "receipt"` (`receipt` | `tax_invoice` | `both`), in `RUNTIME_POLICY`,
  read at the point of use per tenant — never inert. Default `receipt`: a fresh platform issues a
  receipt, not a tax invoice, until it opts into tax.
- `_mark_paid` consults the mode: `tax_invoice`/`both` → finalize the ZATCA e-invoice (as today, via
  the einvoicer driver); `receipt`-only → skip e-invoicing entirely (no ZATCA, no VAT finalize). One
  gate, one place.

### 56.2 — the internal receipt document (non-tax)
- A `receipt` render mode on the PDF renderer: title «Payment Receipt · سند قبض», amount, currency,
  payer (the pseudonymous buyer / account, same rule as the invoice), date, description — and the
  **PAYMENT REFERENCE: `provider` + `provider_id`** pulled from the `Payment`. NO VAT lines, NO seller
  VAT number requirement, NO ZATCA QR. Bilingual, the same premium band/typography.
- The receipt reads its data from the invoice + its paying `Payment`; a receipt is only produced for a
  PAID invoice (it confirms money received).

### 56.3 — serve the right document to the customer
- `GET /api/v1/billing/invoices/{id}/receipt` (billing.self, own invoice only) → the receipt PDF.
- The existing `GET /invoices/{id}/pdf` serves the TAX invoice (mode `tax_invoice`/`both`).
- A convenience `GET /invoices/{id}/document` returns whatever the mode issues (receipt, or tax
  invoice, or — for `both` — links to both), so a client needs to know only "give me the document".
- When a document is not available for the mode (e.g. asking for a tax invoice in `receipt` mode),
  answer a clear 404/409, never a blank or a wrong document.

### 56.4 — carry the gateway reference onto the receipt reliably
- Ensure `provider` + `provider_id` reach the receipt for every paid path: the card charge already
  stores them; confirm the webhook-reconciled and settlement paths do too (a settlement has no
  gateway id — the receipt shows the settlement method/reference instead, never a blank).
- OPTIONAL extra: where the gateway exposes a hosted receipt/invoice URL (Stripe `receipt_url`,
  PayTabs `invoice_link`), capture it into the `Payment` and show it as a secondary "view at
  provider" link — additive, never the primary receipt.

### 56.5 — tests + gates + docs
- Mode matrix: `receipt` (no e-invoice finalize, receipt PDF carries provider_id), `tax_invoice`
  (e-invoice finalized, tax PDF), `both`. Receipt for an unpaid invoice refused. Settlement receipt
  shows the method/reference. The gateway-reference is present and legally retained (erasure leaves
  `payments` untouched — a test pins it). Runtime per-tenant switch. Full suite + security gates.

## Definition of Done

- `BILLING_DOCUMENT_MODE` (receipt | tax_invoice | both), runtime per-tenant (RUNTIME_POLICY), read at
  use; default `receipt`. `_mark_paid` finalizes the ZATCA e-invoice only for tax_invoice/both.
- The internal receipt (non-tax) renders bilingually with the amount, payer, date and the gateway
  `provider` + `provider_id`; produced only for a paid invoice; no VAT/ZATCA.
- The customer fetches the receipt (`/receipt`), the tax invoice (`/pdf`), or "the document"
  (`/document`); the wrong document for the mode is a clear refusal, never a blank.
- The gateway reference reaches the receipt on every paid path (settlement shows method/reference);
  the optional hosted-receipt link is captured only where the gateway actually provides one.
- The transaction reference stays legally retained (erasure never touches `payments`) — pinned by a
  test. Gates + full billing suite green.

## Tasks

- [ ] **56.1** `BILLING_DOCUMENT_MODE` (receipt|tax_invoice|both, default receipt) in config +
  RUNTIME_POLICY; `_mark_paid` finalizes the e-invoice only for tax_invoice/both (skip for receipt).
- [ ] **56.2** Internal receipt render mode (non-tax): amount/payer/date/description + gateway
  provider+provider_id; bilingual; no VAT/ZATCA; only for a paid invoice.
- [ ] **56.3** Routes: `/invoices/{id}/receipt`, keep `/pdf` (tax), add `/document` (mode-aware);
  wrong-document-for-mode → clear 404/409.
- [ ] **56.4** Gateway reference on every paid path (webhook/settlement); optional hosted-receipt
  link captured only for Stripe/PayTabs.
- [ ] **56.5** Tests (mode matrix, unpaid-refused, settlement reference, erasure retains payments,
  runtime switch) + gates.
