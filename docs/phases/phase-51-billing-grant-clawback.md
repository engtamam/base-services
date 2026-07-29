# Phase 51 — billing: claw back the wallet grant on refund and chargeback

When an invoice is paid, billing grants credits to the wallet through a durable outbox
(`grants.py`, keyed `billing:invoice:{id}`). When that payment is later **refunded or charged
back**, nothing takes the credits back: `refund` and `reverse_settlement` touch only the billing
ledger and the ZATCA credit note. The customer gets their money back **and keeps the credits.**

This phase closes that leak — and closes it the way this service already handles money: as a
**durable outbox that reconciles from a fact**, never as an event the bus might drop.

## The belief this phase is built to destroy

> "A refund reverses the money, so the credits the money bought are reversed too — and
> `payment.refunded` tells the wallet to do it."

Both clauses are false. Nothing reverses the credits; and `payment.refunded` is emitted on the
**at-most-once** bus, which `grants.py` was written specifically NOT to trust with money. The event
is a hint. The reversal has to be a promise, the same shape as the grant it undoes.

## What already exists (so we mirror it, not fight it)

- **The grant outbox.** `GrantOutbox.record` writes a row BEFORE delivery; `deliver` calls
  `wallet.credit(source=billing:invoice:{id})`; `reconcile` reads **paid invoices** and heals any
  missing grant. The wallet de-duplicates on `source`. A grant is a durable promise, retried until
  delivered, visible for ever if it cannot be.
- **Void is the durable refund fact.** `refund` already sets the invoice `status="void"` and
  `credit_note=True`; `reverse_settlement` voids too. That flag is the fact a clawback reconciles
  from — the mirror of "paid invoice" for grants.
- **The wallet port** has `credit` only. It has no way to take credits back.

## The work

### 51.1 — a wallet debit the clawback can call — PORT

- Add `debit(*, tenant, fund, amount_minor, source, reason="", allow_negative=False)` to the
  `Wallet` protocol and the HTTP adapter. `allow_negative=True` lets the balance go below zero when
  the credits are already spent (the ask's "allow it to go negative"); with `allow_negative=False`
  an insufficient balance raises `WalletRefused`, which the outbox turns into `grant.clawback_failed`
  rather than a retry-for-ever.
- The `null` wallet debits nothing, like it credits nothing. Disabled-by-default stays disabled.

### 51.2 — the clawback outbox — DURABLE, mirrors the grant

- `GrantOutbox.record_clawback(*, tenant, invoice_id, fund, amount_minor, reason)` writes a reverse
  row with a **distinct** idempotency key `billing:invoice:{id}:clawback`. This is the single most
  important correctness point: reusing `billing:invoice:{id}` would be de-duplicated by the wallet
  as if it were the original credit and reverse nothing. The clawback is its own promise.
- `deliver` for a clawback calls `wallet.debit`. `WalletRefused` (insufficient, `allow_negative`
  off) → `FAILED` + emit `grant.clawback_failed`. Any transport failure → PENDING with the same
  exponential backoff as a grant. No attempt cap: an unreversed clawback is an operator's problem,
  never a silent kept credit.
- `reconcile` gains the mirror sweep: read **voided / credit-noted** invoices whose grant was
  delivered and whose clawback row is missing, and create it. A read is self-healing; a missed
  in-memory step is wrong for ever. (Symmetry with grant-reconcile reading paid invoices.)

### 51.3 — one clawback seam, wired into every reversal path

- A single private `_claw_back(inv)` on `BillingService`, the mirror of "one money truth is
  `_mark_paid`". It reverses the **granted** amount taken from the grant row (`grant_minor` on the
  invoice meta / the recorded grant), NOT the cash refund amount — plan credits are an
  all-or-nothing entitlement and a partial cash refund does not map to fractional credits.
- Called from all three reversal paths so a chargeback settled off-gateway leaks nothing a card
  refund would have caught: `refund`, `reverse_settlement`, and the webhook refund branch
  (`verify_webhook` → `type="refund"`) that reconciles into the void.
- Full clawback fires when the invoice becomes **void**. A partial refund reverses no credits by
  default (knob `BILLING_CLAWBACK_ON_PARTIAL`, default off).

### 51.4 — the edges that make it correct, not just present

- **Expired `reset` grants.** If the plan allowance already lapsed (`expires_at` in the past), the
  credits were never effectively held; clawing back would drive the balance negative for nothing.
  Skip-if-expired behind `BILLING_CLAWBACK_SKIP_EXPIRED` (default on).
- **Fail-safe.** The refund returns the customer's money regardless of wallet reachability — the
  clawback is a durable row processed async, exactly like a grant. The reversal never blocks the
  refund, and is a no-op when no wallet is wired.
- **Idempotent under replay.** A double refund, a redelivered `payment.refunded`, and a reconcile
  sweep all resolve to the one clawback row on `billing:invoice:{id}:clawback`.

## Definition of Done

- `wallet.debit` exists on the port + HTTP adapter + null driver; `allow_negative` respected.
- Clawback is a durable outbox row keyed `billing:invoice:{id}:clawback` (distinct from the grant),
  delivered via `wallet.debit`, retried like a grant, `grant.clawback_failed` on permanent refusal.
- `reconcile` heals a missing clawback from a voided/credit-noted invoice whose grant was delivered.
- One `_claw_back` seam fires from `refund`, `reverse_settlement`, and the webhook refund branch;
  reverses the granted amount, full-on-void, partial only behind a knob.
- Expired `reset` grants are skipped by default; the refund never blocks on the wallet; no-op when
  no wallet is wired.
- Non-functionals: both new knobs read at point of use (no inert knob), wallet host stays
  egress-allow-listed, the clawback path is off any scan path, gates + full billing suite green.

## Tasks

- [ ] **51.1** Wallet `debit(*, allow_negative=False)` on the port, HTTP adapter, and null driver.
- [ ] **51.2** `GrantOutbox.record_clawback` + `deliver`-via-debit + `reconcile` heal from voided
  invoices; distinct key `billing:invoice:{id}:clawback`; `grant.clawback_failed` on refusal.
- [ ] **51.3** `_claw_back` seam wired into `refund`, `reverse_settlement`, and the webhook refund
  branch; reverse the granted amount, full-on-void, `BILLING_CLAWBACK_ON_PARTIAL` (default off).
- [ ] **51.4** Edges + knobs: `BILLING_CLAWBACK_SKIP_EXPIRED` (default on), fail-safe (never blocks
  the refund; no-op with no wallet), RUNTIME_POLICY entries for the two knobs.
- [ ] **51.5** Mutation tests: double-refund idempotency, already-spent → negative vs `FAILED`
  + `grant.clawback_failed`, chargeback-via-`reverse_settlement`, reconcile heals a dropped
  clawback, expired-grant skip, no-op when wallet unwired.
