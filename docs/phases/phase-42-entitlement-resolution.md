# Phase 42 — entitlement resolution

## The idea

**A tenant's entitlements are the UNION of everything they pay for, not the contents of one row.**

`_active_limits` resolves a single subscription — `active_for()` is `limit=1, sort=created desc` —
and uses only that row's `limits_snapshot`. A tenant who buys three things is entitled to one of
them, and which one depends on insertion order.

The refusal is a fail-closed 402 that is **indistinguishable from "you have not paid for this"**,
which is what makes it expensive: the customer, the support engineer and the operator all read it
as a billing problem with the customer. The team who reported it spent a while assuming their own
seeding was wrong.

This is not only a bug. It encodes an assumption — one subscription per tenant, scaled by
`quantity` — that is one of several real billing shapes, and the others are ordinary:

| shape | what it looks like | works today? |
| --- | --- | --- |
| one plan, seats | `quantity: 25` on one subscription | yes |
| per-item catalogue | one subscription per product/standard | **no** |
| add-ons | a subscription granting only a delta | **no** |
| prepaid packs | a one-off purchase topping up a counter, never renewing | **no** |
| enterprise contract | fixed term, negotiated caps, no recurring plan | **no** |

## Decisions

**Merge strategy belongs to the KEY, not to the deployment.** A global "sum or max" switch breaks
on the first mixed catalogue: `seats` sums, `max_retention_days` takes the larger, `sso_enabled`
ORs, and `support_tier` takes the highest-ranked value — which is neither sum nor max. So the
strategy is declared beside the limit, defaulting to `SUM` for numbers and `OR` for booleans, and
a platform overrides per key. Nothing is assumed about a limit whose strategy nobody declared: it
takes the default for its type and says so in the answer.

**Provenance carries the REASON, not just the winner.** `{"value": 25, "strategy": "sum",
"from": [{"subscription": "sub_a", "contributed": 20}, …]}`. "Why is this denied?" becomes
answerable without reading the database — and so does the harder question a customer actually
asks, which is "I paid for 25, why do I have 20?"

**Past-due contributes, but only inside the existing grace window.** A late bank transfer must not
switch off a hospital's cameras; a customer who stopped paying two months ago is not entitled.
`active`, `trialing`, and `past_due` while `grace_days` has not elapsed. It is config, because
this is a credit decision and a platform's risk appetite is not ours to set.

**Add-ons STACK by default.** That is what the word means, and it is the safe direction: a wrong
stack overcharges nobody, a wrong replace silently removes something a customer paid for. `replace`
is available per plan for tiers that genuinely supersede.

**Grandfathering applies per subscription, BEFORE the merge.** Each row's signup terms win for that
row. Merging first and then grandfathering would apply one subscription's historic price protection
to another subscription bought yesterday.

**A cap is not always a wall.** Today exceeding a limit is a refusal. Many contracts want the
excess metered and billed. Per key: `refuse | meter | warn`. Defaults to `refuse`, because the
alternative silently spends a customer's money.

## What must never happen

1. **A paying customer refused.** The failure mode that started this. Every existing
   single-subscription tenant must resolve **byte-identically** to today — asserted, not assumed.
2. **A non-paying tenant granted.** A cancelled or long-past-due subscription must contribute
   nothing, and an unknown merge strategy must never widen an entitlement.
3. **A silent answer.** An entitlement decision with no provenance is one nobody can dispute or
   defend.
4. **A slow answer.** This is the hot path — feature-flag-service calls it per flag evaluation.
   Resolving N subscriptions must not become N queries.

## Tasks

| # | task |
| --- | --- |
| 42.1 | `active_for()` → `billable_for()`: every subscription in a configurable status set, indexed, one query |
| 42.2 | Grace-window handling for `past_due`, driven by the existing `grace_days` |
| 42.3 | Merge strategies as declared data: `SUM`, `MAX`, `MIN`, `OR`, `AND`, `RANK`, `REPLACE` |
| 42.4 | Strategy resolution per key — plan declaration first, type default second, never an assumption |
| 42.5 | Provenance in the answer: value, strategy, and each contributing subscription |
| 42.6 | Add-on plans: `stack` (default) vs `replace`, declared per plan |
| 42.7 | Per-key cap policy: `refuse` / `meter` / `warn` |
| 42.8 | Prepaid packs: a non-renewing purchase that tops up a counter |
| 42.9 | Contract terms: fixed-term negotiated caps with no recurring plan |
| 42.10 | Hot path: one indexed read, generation-keyed cache, no N+1 |
| 42.11 | **Byte-identical resolution** for every existing single-subscription shape |
| 42.12 | Adversarial tests: overlapping periods, mid-cycle changes, a cancelled add-on, conflicting strategies, an unknown key |
| 42.13 | Mutation-test every strategy and the status set; a gate that cannot fail is decoration |
| 42.14 | Phase doc, full gate run |

## Definition of done

- Every existing tenant resolves identically — proven by test, not by inspection
- No unwired knob: every setting introduced here is read on a real path, enforced by the knob-guard
- Provenance on every answer, including refusals
- Mutation-tested: each merge strategy, the status set, and the grace window each break a test
- 28 gates green

## Honest limits

- **This does not make billing multi-currency across subscriptions.** Two subscriptions in
  different currencies cannot be summed, and pretending otherwise would invent money. That case
  refuses and says why.
- **Usage is still enforced atomically at `usage/consume`**, not by this resolver. A merged cap is
  the number the atomic check compares against; it is not itself a counter.
- **`RANK` needs an ordered vocabulary** (`bronze < silver < gold`), which is per-platform config.
  Without one, a rank-strategy key falls back to refusing rather than guessing an order.
