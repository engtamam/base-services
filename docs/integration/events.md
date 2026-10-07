# Domain events, and what you may build on them

Services publish events so you can react without them knowing you exist. Before you subscribe to
anything, read this section — it is the difference between an integration that works and one that
loses money quietly.

## Delivery is at-most-once. Best effort. There is no guarantee.

There is no outbox, no durable subscription, no retry, no dead-letter and no replay. `publish()`
fans out to whoever is subscribed *at that moment* and swallows their exceptions. A subscriber that
is restarting, disconnected, or simply not deployed yet does not receive the event, and **nothing
anywhere records that it missed one.**

So the rule for every consumer is:

> An event is a **hint** that makes a reconciliation faster.
> It is **never** the only way a fact can be learned.

If the only way your service can discover a payment succeeded is by receiving `payment.succeeded`,
then a pod restart at the wrong second means it never learns. Not for a while — for ever, because
nothing will say it again.

**Every fact you depend on must also be readable**: a status field, a tombstone, an endpoint you can
poll. A read is self-healing; a missed event is wrong for ever.

## What this means in practice

| you want to | do NOT | do |
|---|---|---|
| credit a wallet when someone pays | subscribe to `payment.succeeded` and credit | let billing's grant outbox deliver it — durable and retried (below) |
| show "recently paid" in a UI | rely on the event alone | subscribe for freshness, read the invoice list on load |
| suspend on non-payment | act on `payment.failed` alone | act on it, and reconcile from subscription status on a schedule |
| warn on a low balance | assume `wallet.balance_low` always arrives | also read `low_balance` on the balance response |

## Events published today

### billing-service

| event | when | key fields |
|---|---|---|
| `invoice.created` | an invoice is raised for a subscription and is NOT yet paid (before any charge) | `invoice_id`, `subscription_id`, `plan_id`, `amount_minor`, `currency`, `due_at`, `period_start`, `period_end` |
| `invoice.paid` | an invoice becomes paid, by any route | `invoice_id`, `amount_minor`, `paid_at` |
| `payment.succeeded` | same moment — the money is recorded | `payment_id`, `tenant`, `amount_minor` |
| `subscription.expiring_soon` | N days before a period ends (daily sweep, once per period) | `subscription_id`, `plan_id`, `period_end`, `days_remaining` |
| `payment.failed` | a charge failed | `attempts`, `final`, `next_retry_at` |
| `payment.refunded` | a refund completed | `payment_id`, `amount_minor` |
| `subscription.renewed` | a billing period rolled | `period_key`, `period_start`, `period_end` |
| `subscription.cancelled` | a cancellation was recorded | `at_period_end`, `current_period_end` |

Notes that matter:

- **`payment.succeeded` fires from the one place an invoice becomes paid**, so a card charge, a
  webhook reconcile and an off-gateway settlement all produce the same event. Your view of "paid"
  does not depend on how the money arrived.
- **`payment.failed` carries `final`.** `false` means a retry is scheduled. Treating every failure
  as final dunning-mails a customer whose retry would have succeeded an hour later.
- **`subscription.cancelled` carries `at_period_end`.** `true` means keep serving them until the
  period ends. A consumer that cannot tell the two apart either cuts access early or never cuts it.
- **`subscription.renewed` carries `period_key`** — the idempotency key the billing cycle itself
  used. Key your own work on it and a replay cannot double-apply.
- **`invoice.created` is the "awaiting payment" hint, fired before any charge** — use it to send a
  pay-link or reminder the moment an invoice becomes due. It is a hint, not money: the invoice is the
  durable fact (read it back via `GET /internal/billing/invoices/{invoice_id}`), and `invoice.paid` /
  `payment.succeeded` fire when the money lands. A consolidated invoice spans several subscriptions, so
  it carries empty `subscription_id`/`plan_id` and a `consolidated: true` flag — key on `invoice_id`.
- **`subscription.expiring_soon` fires exactly once per period** (keyed on subscription + period end)
  and re-arms when the period rolls forward on renewal. `days_remaining` is whole days rounded up. It
  is the proactive counterpart to `subscription.renewed`: a prompt BEFORE access lapses, not after the
  roll. Controlled by `SUBSCRIPTION_EXPIRY_WARN_DAYS` (default 3; `0` disables).

### wallet-service

| event | when |
|---|---|
| `wallet.credited` / `wallet.spent` | funds moved |
| `wallet.balance_low` | the balance **crossed** below the configured threshold |
| `wallet.balance_recovered` | it climbed back |

`wallet.balance_low` is **edge-triggered**: it fires on the crossing, once, and re-arms only after
recovery. An alert that fires on every spend is noise, and noise gets muted. The state is also on
the balance read (`low_balance`), because the event may be lost.

### feature-flag-service

`flag.exposed` (what makes an A/B experiment measurable) and `maintenance.changed`.

## Payloads carry no personal data

A payload lands in logs and in every subscriber's store, including subscribers with no lawful basis
to hold a person's details. Events carry a `tenant` and the relevant ids — enough to look up
anything more from a service that is allowed to give it to you.

## The exception: money that must not be lost

Credits a customer paid for are **not** delivered by event, for the reason at the top of this page.
billing-service writes every grant down before delivering it and retries until the wallet
acknowledges, keyed on the invoice so a retry credits once. If billing crashes between the payment
and the grant, `POST /internal/billing/grants/reconcile` rebuilds the promise from the paid invoice
— the durable fact.

Turn it on with `WALLET_DRIVER=http` plus `WALLET_SERVICE_URL` (whose host must be in
`EGRESS_ALLOWED_HOSTS`, since that URL carries an internal token). It is off by default: an adopter
who does not run wallet-service should not acquire a background task failing against a service they
never deployed.

Schedule these two:

```
POST /internal/billing/grants/drain       # deliver what is due
POST /internal/billing/grants/reconcile   # rebuild anything a crash lost
```

## Declaring your own

`GET /internal/manifest` lists each service's `events.emitted` and `events.consumed`. That is the
only place an event can be discovered, so **an undeclared event reaches nobody** — operationally
identical to not publishing it. The `safety:event-contract` gate fails the build when a service
publishes a named event it has not declared, or declares one nothing publishes.
