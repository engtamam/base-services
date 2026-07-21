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

## The trap to design out

**A credit granted twice is money given away.** Event delivery is at-least-once, so the wallet's
handler has to be idempotent on the payment's identity, not on the event's — a redelivered
`payment.succeeded` and a genuine second payment of the same amount look identical otherwise. This
is the same shape as the erasure-receipt defect: the system stays internally consistent and is
collectively wrong.

**A low-balance event that fires every spend is noise, and noise gets muted.** It must fire on the
*crossing*, once, and re-arm only after the balance recovers.

## Tasks

- [ ] **45.1** billing emits domain events: `payment.succeeded`, `payment.failed`, `subscription.renewed`, `subscription.cancelled`, `creditpack.settled` — declared in the manifest, since an undeclared event is one no consumer can discover
- [ ] **45.2** A gate: a service that publishes an event must declare it in `emitted`, and a service that subscribes must declare it in `consumed` — the manifest is the contract or it is decoration
- [ ] **45.3** wallet-service consumes `payment.succeeded` / `creditpack.settled` and credits the balance, idempotent on the **payment identity** so a redelivery cannot double-credit
- [ ] **45.4** Plan lifecycle: edit price/limits, deactivate, hide from signup — with existing subscriptions pinned to the terms they were sold, because silently repricing a live subscription is the one outcome that must never happen
- [ ] **45.5** wallet low-balance threshold (per tenant, runtime-editable via Phase 44's surface) emitting `wallet.balance_low` **on the crossing only**, re-arming on recovery
- [ ] **45.6** Plans grant their included credits on renewal, through the same idempotent path as 45.3
- [ ] **45.7** Mutation-test the idempotency and the re-arm: a double-credit test that cannot fail is the only kind of test that matters here
- [ ] **45.8** Full gate run, and the event contract documented for adopters
