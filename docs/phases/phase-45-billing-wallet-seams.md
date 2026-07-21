# Phase 45 — the billing/wallet seams

Four gaps, all verified against the code before being written down. They are one phase because they
are one story: **billing knows things happen and tells nobody.**

## What is actually true today

| # | Gap | Evidence |
|---|-----|----------|
| 1 | billing-service emits no domain events | `manifest.py` → `"events": {"emitted": [], "consumed": []}`. Nothing publishes. |
| 2 | Nothing wires billing to wallet | "wallet" appears in billing only as a datastore collection name. A settled credit-pack never credits a balance; a paid renewal never grants a plan's included credits. |
| 3 | Plans are immutable | `api/v1/billing.py` has `POST /plans` and `GET /plans` and nothing else. An operator cannot correct a price, retire a plan, or hide it from signup. |
| 4 | wallet-service has no low-balance signal | No threshold, no event. The only "you are low" is a `402` at spend time — which is not a warning, it is an outage for whoever was mid-task. |

## Why this is one phase, and why it is the plug-and-play problem

An adopter buys these services to avoid building billing. What they get today is a service that
records money correctly and then keeps it to itself. Every integration they must still write —
credit the wallet on payment, warn before a balance runs out, react to a renewal — is a place the
plug-and-play promise stops being true, and each one is written differently by every adopter.

**The fix is events, not a wire.** A direct billing→wallet call would couple two services that are
deliberately separate and would still leave every other consumer unserved. Billing publishes what
happened; wallet subscribes to what it cares about. An adopter who wants different behaviour
subscribes instead of patching.

Gap 2 is therefore a *consequence* of gap 1, and must not be fixed by reaching across.

## The trap to design out — corrected after reading the event bus port

I first wrote this phase assuming delivery is at-least-once, so the danger was double-crediting.
**That was wrong, and the truth is worse.** `app/ports/event_bus.py` says plainly:

> DELIVERY IS AT-MOST-ONCE. BEST EFFORT. THERE IS NO GUARANTEE.
> An event is a HINT that makes a reconciliation faster. It is NEVER the only way a fact can be
> learned.

There is no outbox, no retry, no replay. A subscriber that is restarting when the event fires never
receives it and nothing records that it missed one.

So **crediting a wallet purely from a `payment.succeeded` subscription is a money-loss bug.** A pod
restart at the wrong second means a customer paid and their credits never arrive — not late, never,
because nothing will ever tell wallet again. That is the erasure-receipt shape once more: every
record internally consistent, the whole collectively wrong.

### What this forces

Two paths, not one, and they are for different jobs.

| | purpose | may be lost? |
|---|---|---|
| **event** (`payment.succeeded` …) | tells any consumer something happened, so anyone can react without billing knowing they exist | yes — it is a hint |
| **grant** (durable, retried, idempotent) | the *money consequence*: credits the customer paid for | no |

The grant path uses wallet's existing `POST /internal/wallet/credit`, keyed on the **payment id**,
and is retried until wallet acknowledges. Idempotency is still required — not because the bus
redelivers, but because the retry does, and because a reconciliation sweep re-reads the same
payments. A credit granted twice is money given away; a credit granted never is money taken.

This does not resurrect the billing→wallet coupling I argued against. The objection was to a direct
synchronous call *instead of* events, which serves exactly one consumer and hides the fact from
everyone else. Billing still publishes for everyone; it additionally guarantees the one consequence
that must not be lost. And the grant path is off unless configured, so an adopter not using
wallet-service is not forced into it.

**A low-balance event that fires every spend is noise, and noise gets muted.** It must fire on the
*crossing*, once, and re-arm only after the balance recovers.

## Every business rule here is a choice, not a decision

This is a general platform. Where two adopters would reasonably want different behaviour, the
answer is a config knob with a safe default — never one behaviour baked in, and never a question
put to the person building the platform.

| knob | default | the other option, and why someone wants it |
|---|---|---|
| `PLAN_REPRICE_MODE` | `on_renewal` | `immediate` — a platform whose plans are metered utilities, where the current rate is the rate |
| `PLAN_DEACTIVATE_MODE` | `hide_from_signup` | `end_at_renewal` — retiring a plan for everyone rather than only for new customers |
| `WALLET_CREDIT_ON` | `payment.succeeded` | `settlement` — where funds are only real once the processor settles |
| `WALLET_LOW_BALANCE_THRESHOLD` | `0` (off) | any amount; per tenant, runtime-editable |
| `WALLET_LOW_BALANCE_MODE` | `crossing` | `every_spend` — for platforms that want a running nag |
| `PLAN_CREDIT_GRANT_MODE` | `reset` | `accumulate` — do unused included credits roll over? |
| `EVENT_PUBLISH_FAILURE` | `fail_closed` | `fail_open` — where a missed event is preferable to a refused payment |

Defaults chosen so that the surprising outcome is the one you opt into. Repricing someone silently,
double-granting credits, or nagging on every spend are all things a platform should have to ASK for.

## Tasks

- [x] **45.1** billing emits domain events: `payment.succeeded`, `payment.failed`, `subscription.renewed`, `subscription.cancelled`, `creditpack.settled` — declared in the manifest, since an undeclared event is one no consumer can discover
- [x] **45.2** A gate: a service that publishes an event must declare it in `emitted`, and a service that subscribes must declare it in `consumed` — the manifest is the contract or it is decoration
- [x] **45.3** The durable grant path: billing credits the wallet through `POST /internal/wallet/credit` keyed on the PAYMENT id, retried until acknowledged — because an event may be lost and a paid-for credit may not. Off unless configured.
- [x] **45.4** Plan lifecycle: edit price/limits, deactivate, hide from signup — with existing subscriptions pinned to the terms they were sold, because silently repricing a live subscription is the one outcome that must never happen
- [x] **45.5** wallet low-balance threshold (per tenant, runtime-editable via Phase 44's surface) emitting `wallet.balance_low` **on the crossing only**, re-arming on recovery
- [x] **45.6** Plans grant their included credits on renewal, through the same idempotent path as 45.3
- [x] **45.7** Mutation-test the idempotency and the re-arm: a double-credit test that cannot fail is the only kind of test that matters here
- [x] **45.8** Full gate run, and the event contract documented for adopters
