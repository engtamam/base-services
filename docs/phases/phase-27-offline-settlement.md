# Phase 27 — Off-gateway settlement (billing-service)

> Twelve gateway adapters, and no way to record a payment that never touched a gateway.
> A Saudi enterprise buys on a purchase order and pays by bank transfer 30–60 days later,
> against a tax invoice. Above ~SAR 100k nobody pays by card. The invoice stays `open` forever.
>
> This is **not** a Saudi feature and **not** a bank-transfer feature. It is *settle an invoice
> from a payment that happened somewhere else* — wire, cheque, cash, an offline card terminal.
> Every B2B SaaS on this service hits it on its first enterprise deal.

The request came from a team who worked around it by tracking payment state behind their own
port. They are right to hate that: it splits the truth about money across two systems, which is
the thing billing-service exists to prevent.

---

## 0. The gap (verified, with lines)

| # | Finding | Evidence |
| --- | --- | --- |
| 1 | `_mark_paid()` is **private**. The only paths to it are a gateway charge, a `0`-total invoice, and a webhook reconcile. | `app/core/billing_service.py:334`, callers at `:312 :327 :394 :419` |
| 2 | `RecordingGateway` records the intent and **never settles** — deliberately, honestly (`settled=False`). It is a pilot mode, not an offline-payment mode. | `app/adapters/gateway/recording.py` |
| 3 | `Invoice` has no notion of a **partial** payment: `status` goes `open -> paid`, with no `settled_minor`. A 60-day enterprise paying in two tranches cannot be represented. | `app/core/models.py:108-123` |
| 4 | `_mark_paid` stamps `paid_at = iso()` — **now**, not when the money actually moved. | `billing_service.py:335-336` |

(3) and (4) are not in the request. They are why a naive fix ships a wrong tax invoice.

---

## 1. Definition of Done — answered BEFORE any code

### (1) Hot path

**No — and pretending otherwise would be theatre.** Settlement is a low-frequency admin or
webhook action; a cache would protect nothing. But it carries a **concurrency** requirement
sharper than any hot path: two admins settling the same invoice, or two tranches of one wire
landing together, must not both finalize the e-invoice and both write a ledger row.

So `settled_minor` accumulates through Phase 25's atomic `incr_if_below`, bounded by
`total_minor`. The primitive that stops the 26th seat stops the second settlement.

* **Benchmark:** 50 concurrent settlements of one invoice (distinct `(method, issuer,
  reference)` triples, each for the full amount) ⇒ **exactly one succeeds**,
  `settled_minor == total_minor`, **exactly one** `paid` transition, **one** e-invoice,
  **one** ledger row. The other 49 are refused, and nothing is written for them.
* **Benchmark:** the same triple replayed against the **same** invoice ⇒ the original result,
  `idempotent: true`, charged once. Against a **different** invoice ⇒ a generic refusal — one
  transfer cannot settle two invoices. A **reversed** settlement is never replayed.

### (2) Every config knob has a consumer

* `SETTLEMENT_METHODS` — the plug-and-play core:
  `bank_transfer:issuer,cheque:issuer,offline_card:issuer,cash:no-issuer`.
  Each entry is `name:issuer-policy`. **No method is ever named in code**; it is an allowlist,
  and an unknown method — or a missing issuer for a method that requires one — fails closed.
  A project that settles by carrier pigeon adds `pigeon:no-issuer` and ships.
* `SETTLEMENT_ALLOW_PARTIAL` — whether an invoice may be settled in tranches.
* `SETTLEMENT_TOLERANCE_MINOR` — a correspondent bank takes its fee out of the wire; a
  SAR 100 000 invoice arrives as 99 980. Without a tolerance, every enterprise invoice on the
  platform stays `open` over 20 halalas.
* `SETTLEMENT_MAX_BACKDATE_DAYS` — `paid_at` is the bank's value date, so it is in the past.
  Unbounded backdating rewrites closed tax periods.

`test_every_settings_field_is_consumed_somewhere` enforces the lot. No knob before its consumer.

### (3) What production needs that the request omits

| Need | Why |
| --- | --- |
| **Partial settlement** (`settled_minor`, `partially_paid`) | a 60-day enterprise pays in tranches; two POs against one invoice |
| **`paid_at` = the bank's value date** | `_mark_paid` stamps `now()`. The tax invoice would be filed in the wrong period, and revenue recognised in the wrong month |
| **Bounded backdating** | the value date is in the past; unbounded, it rewrites a closed ZATCA period |
| **Fee tolerance** | the wire arrives short by the correspondent bank's fee |
| **Currency must match** | a USD wire cannot settle a SAR invoice; refuse, never convert silently |
| **Reversal (recall / bounce)** | a wire *can* be recalled. Never delete a ledger row or an e-invoice — issue a credit note, exactly as a card refund does |
| **Proof by reference** | the bank advice PDF lives in `file-service`; billing stores an id, never a blob |
| **An `actor`** | the internal token is a *service*. Somebody read a bank statement and decided. Audit needs their name |
| **Dunning stops only when FULLY paid** | a partial payment must not silence the reminders |
| **E-invoice finalized exactly once** | never per tranche — ZATCA gets one invoice, when it is settled |

### (4) The correctness footgun

**What identifies one real-world payment.**

An earlier draft of this document said the reference must be *globally unique*. That was
**wrong**, and the requesting team caught it: a bank reference is unique **per bank, not per
planet**. Two banks can issue the same string, and global uniqueness would reject the second
one — a real, valid payment, refused.

Their first proposal, `(method, reference)`, fails by the same argument they raised: two banks,
both `method=bank_transfer`, same string. So the identity of a payment is the **triple**:

```
(method, issuer, reference)
```

`issuer` is the institution that minted the reference — the bank, the clearing house, the
terminal. It is what makes the reference mean anything.

**But `issuer` cannot be blindly required.** Cash has no issuer. Neither does an internal
write-off. So whether a method carries an issuer is declared **with the method**, in config,
never as a branch in code:

```
SETTLEMENT_METHODS="bank_transfer:issuer,cheque:issuer,offline_card:issuer,cash:no-issuer"
```

For an issuer-less method the tenant is the issuer, which keeps the triple total: a cash
receipt numbered `0001` in two tenants is two different payments, and correctly so.

**And scoping the key does not fix the second problem.** Whatever the key, an error that says
"already used" turns settlement into an **oracle**: probe a triple, learn from the response
whether another tenant received that transfer. So the error is generic —
`SETTLEMENT_ALREADY_RECORDED`, never *by whom*, never *against which invoice*. The tenant that
owns the original settlement can see it; nobody else learns it exists. (Same shape as
`_owned_report` returning 404 rather than 403.)

Idempotency is unchanged by any of this, and it is answered **before** any state check: by the
time a caller retries, the invoice may already be `paid`, and validating first would reject the
very retry we promised to honour. A retry of the same triple against the same invoice returns
the original settlement; against a different invoice it is refused; a settlement that was
**reversed** is never replayed — the bank took that money back.

Runner-ups, each tested: a partial payment finalizing the e-invoice · `paid_at` in the future ·
settling a `void`/`draft`/`paid` invoice · a settlement exceeding the total · a reversal that
deletes history · a currency mismatch · a method not on the allowlist · an issuer omitted for a
method that requires one · the same reference from two different issuers (**must both settle**).

---

## 2. Model

```jsonc
// Invoice gains two fields; `status` gains one value.
{ "settled_minor": 0,                     // sum of confirmed settlements
  "status": "draft|open|partially_paid|paid|uncollectible|void" }

// A settlement is an immutable record, mirroring Payment.
{ "id": "stl_…", "tenant": "…", "invoice_id": "inv_…",
  "method": "bank_transfer",              // from SETTLEMENT_METHODS, never hardcoded
  "issuer": "SABB",                       // the bank/clearing house that minted the reference
  "reference": "MT103-2026-0042",         // unique per ISSUER, not per planet
  "amount_minor": 9998000, "currency": "SAR",
  "paid_at": "2026-07-01T00:00:00Z",      // the BANK's value date
  "proof_file_id": "file_…",              // file-service; never a blob here
  "actor": "finance@acme.com",            // a person, not a service token
  "note": "PO-4471",
  "status": "confirmed|reversed" }
```

The identity of one real-world payment is `(method, issuer, reference)`. For a method declared
`no-issuer`, the tenant stands in as the issuer, so the triple is always total. Two banks may
legitimately mint the same reference string, and both settlements must succeed.

`_mark_paid` is refactored, not exposed: settlement flows into the **same** private path a card
charge takes, so entitlements, ZATCA e-invoicing and dunning behave identically. That is the
whole point of the request — one truth about money.

## 3. API

Internal (service-to-service, e.g. a bank-feed reconciler):

| Method | Path | Semantics |
| --- | --- | --- |
| POST | `/internal/billing/settlements` | record a settlement; idempotent by `(method, issuer, reference)` |
| POST | `/internal/billing/settlements/{id}/reverse` | a recalled wire; issues a credit note |

User tier (`billing.manage`, an actual human with a capability):

| Method | Path |
| --- | --- |
| POST | `/api/v1/billing/invoices/{id}/settle` |
| GET | `/api/v1/billing/invoices/{id}/settlements` |

Returns `{invoice_status, settled_minor, remaining_minor, einvoice_finalized}` so a UI never
has to compute what is owed — the mistake that produced caller-supplied `current` in Phase 25.

## 4. Steps

| # | Step |
| --- | --- |
| 27.1 | `core/settlement.py` — pure: method+issuer-policy allowlist, the `(method, issuer, reference)` identity, tolerance, backdate window, amount rules |
| 27.2 | Models: `Settlement`, `Invoice.settled_minor`, `partially_paid` |
| 27.3 | `SettlementStore` + the `(method, issuer, reference)` index (unique, GENERIC error — the key is not an oracle) |
| 27.4 | `settle()` — atomic `incr_if_below(settled_minor, bound=total_minor)`; e-invoice + ledger exactly once, on the transition to `paid` |
| 27.5 | `paid_at` from the bank's value date; `_mark_paid(paid_at=…)` |
| 27.6 | `reverse()` — credit note, never a deletion |
| 27.7 | Routes + manifest + capabilities + audit (`actor`) |
| 27.8 | Tests: 50-concurrent-settle ⇒ 1 paid / 1 e-invoice / 1 ledger row · replayed triple = 0 writes · **same reference from two different issuers BOTH settle** · same triple across tenants refused with a GENERIC error (no oracle) · a `no-issuer` method scopes to the tenant · issuer omitted where required → fail closed · partial → no e-invoice · tolerance · backdate bounds · currency mismatch · unknown method · reversal keeps history · dunning stops only when fully paid · knob guard |
| 27.9 | Verify + gate + live boot + docs + memory |

## 5. Out of scope

Bank-feed ingestion (a connector-service job; this service accepts the settlement, it does not
go fetch it). Payment reconciliation UI. FX conversion — a currency mismatch is refused, never
silently converted.

---

## 6. Corrections to this document

**The identity key was wrong in the first draft, and the requesting team fixed it.**

I wrote that the bank reference must be *globally unique*. A bank reference is unique **per
bank, not per planet**: two banks can mint the same string, and global uniqueness would have
refused the second — a real payment, rejected, in production, on an enterprise invoice.

Their first suggestion, `(method, reference)`, fails to the same objection they raised (two
banks, both `bank_transfer`). The key is `(method, issuer, reference)`, which they also
proposed. It is what shipped in §2.

What survives from the original draft is the part the nuance does not touch: **scoping the key
does not close the oracle.** Whatever the key, an error that names the owner of an existing
settlement leaks the existence of another tenant's payment. The error is generic.

What the nuance *created*: `cash` has no issuer, so `issuer` cannot be blindly required — the
issuer policy is declared with the method, in config, never as a branch in code.

---

**STATUS: ✅ ALL DONE — 296 billing tests (261 pre-existing still green → full back-compat), gate PASS, live verified, and the concurrency guarantees VERIFIED ON REAL POSTGRESQL (16.4, docker).**

Built: `core/settlement.py` (pure; method+issuer-policy allowlist, the `(method, issuer, reference)` identity, value-date bounds, tolerance, amount rules) · `Settlement` model + `Invoice.settled_minor` · `SettlementStore` with an atomic identity claim · `settle()` / `reverse_settlement()` / `settlements_for()` · `_mark_paid(paid_at=…)` refactored so a wire lands on the SAME path as a card · routes + manifest.

**No new port primitive.** The identity claim is `incr_if_below(claims, key, 1, 1)` — a 1-of-1 counter, the same operation that stops the 26th seat. `settled_minor` accumulates through the same call, bounded by `total_minor`.

**Measured on real PostgreSQL:** 50 concurrent settlements of one invoice → **1 winner, 1 e-invoice, 1 ledger row**; 10 concurrent tranches of 30 000 against a 100 000 invoice → **exactly 3 accepted, counter = 90 000**; one wire raced against 20 invoices → **1 settled, 19 refused**; with a tolerance, two settlements that both observe `closes` → **exactly one finalizes**.

## 7. The bug only PostgreSQL could show

The first implementation wrote `inv.settled_minor = settled_after` onto the invoice row after each settlement. That is a **lost update**: three concurrent tranches each read-modify-write the same row and the last writer wins with a stale value. Postgres reported `settled_minor = 60 000` where three tranches of 30 000 had been accepted. Worse, a late *partial* writer could overwrite a `paid` invoice back to `partially_paid`.

The in-memory adapter never showed it — its lock serialises writers instead of letting them race, exactly as it hid the wallet's CAS contention in Phase 26.

**The fix was to stop storing what can be derived.** The atomic counter is the truth; `partially_paid` is computed from it; and the *only* invoice write during settlement is the single `paid` transition, guarded by its own 1-of-1 claim (`closed:{invoice_id}`). A reversal releases that claim, because the invoice is open again and a later settlement must be able to close it.

Two tests now pin it: `test_the_counter_is_the_truth_not_the_invoice_row` and `test_a_late_partial_settlement_cannot_downgrade_a_paid_invoice`.

**Also corrected:** `settlement.check_paid_at` refuses a value date before the invoice was issued — my first smoke test paid an invoice three days *before* it existed, and the rule was right while the test was wrong.

## 8. One more thing the requester was right about

Their nuance ended *"Still idempotent on retry."* The first implementation of this phase refused
a retry of the same triple, because it validated the invoice's status **before** consulting the
payment identity — and by the time a caller retries, the invoice is already `paid`. The retry
they explicitly asked for would have failed.

Fixed: the identity is resolved first. Same triple, same invoice ⇒ the original result. Same
triple, different invoice ⇒ generic refusal. A **reversed** settlement is never replayed, since
resurrecting it would re-settle an invoice with money the bank has recalled.
