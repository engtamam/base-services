# Phase 40 — verification-service

## Why it exists

We had zero identity verification. `grep -i "nafath|wathq|kyc"` across all 18 services returned
nothing.

That is not a missing nice-to-have; it is the foundation the *next* phase stands on. The anti-abuse
control for a GCP-style welcome gift is **one gift per commercial registration** — and both
reference implementations studied (`ammar`, `Security-tools`) collect the CR number and never verify
it. One says so in a comment. Their strongest anti-fraud control rests on unverified input, so the
gift is farmable with invented registration numbers.

Building onboarding first would have shipped that same hole and retrofitted the fix.

## The failure this service is shaped around

A **false VERIFIED**. Everything else it can get wrong is recoverable: a wrongly refused customer
complains and gets re-checked; an outage retries. A wrongly verified identity is trust granted to
whoever asked for it, and nothing downstream can detect it — the gift issues, the account opens,
and the audit log faithfully records that we verified them.

Every design decision below follows from that.

## Decisions

**`refused` and `error` never merge.** They look alike from a caller's chair and they are opposites:
refused is an answer, error is the absence of one. Merged, you must pick one behaviour for both —
block on error and a provider outage locks out every legitimate customer; allow on refused and you
admit exactly the person the check exists to stop. `is_verified` is `status == VERIFIED`, never
`!= REFUSED`, so a status added later defaults to untrusted.

**Every provider is config, not code.** One HTTP adapter, config-mapped: url, auth, request shape,
and where the verdict lives in the response. Nafath, Wathq, Companies House, VIES, BankID are
profiles. This is the plug-and-play answer *and* the honest one — we hold no verified API contract
for the Saudi providers, and a hand-written `nafath.py` with invented endpoints would look
authoritative, pass every test written against our own invention, and fail on first contact.

**Unknown is never yes.** Missing verdict field, unreadable body, HTTP error, a value nobody
configured: all `error`. There is no path where not-knowing becomes approval.

**The raw identifier is never stored.** A verification store is a concentrated pile of exactly what
an attacker wants. What persists is `HMAC(PII_SALT, claim_type|normalised_value)` plus the verdict
— the store cannot leak identities even if read in full, a property no access control provides.
Normalisation folds separators and Arabic-Indic digits, because an anti-abuse control a space
defeats is not one.

**`claim_ref` is scoped by claim type, NOT by tenant** — the one place this platform's pseudonym
rule is deliberately inverted. The anti-abuse question is "has this business been given a gift
*anywhere*", which a tenant-scoped hash cannot answer. That is a real privacy cost taken knowingly,
and it is why `claim_ref` never leaves the service: handing it out would let a caller correlate one
person across tenants, the exact linkage the hashing exists to contain.

**Expiry is evaluated on read, not by a sweeper.** A cancelled registration must stop reading as
verified the moment its TTL lapses — a sweeper that has not fired yet is a window in which a stale
verdict is still trusted, and that window is when someone would use it. A stale verdict reports as
`expired`, not `refused`: the caller needs to know whether to re-ask or to stop.

**Manual review is a first-class adapter.** Upload a certificate, a human decides. Works in any
market on day one with no external contract, so a market we have no provider for is not a market
where identity stops mattering. It is also the highest-privilege operation here — an automated
verifier can only repeat a registry, a reviewer can approve anyone — so both directions are audited
and a reviewer cannot decide their own claim.

**`identity_seen` survives erasure, and the ack says so.** Delete the tenant, sign up with the same
registration, no second gift — per-tenant idempotency cannot see that because the tenant is gone.
The row is a one-way hash with a date and a count: no name, no contact, not reversible. Phase 37's
rule was *say what you kept and why*; this is that rule applied where it is least comfortable.

## What the tests caught

**A two-eyes bypass in my own code.** Written as `reviewer not in prior`, a reviewer approving a
*second* time fell through to the decision — one pair of eyes satisfying a two-eyes control. The
control is two *people*, not two clicks.

**A tier mistake, caught by `safety:action-coverage`.** The identity-seen lookup sat on the user
tier behind a capability, when its only caller is onboarding-service asking machine-to-machine. A
capability check was standing in for machine auth. Moved to the internal tier.

**A double route prefix** — `/api/v1/api/v1/verification/…` — the exact wart the Kobo review flagged
as "worth designing around".

Mutation-tested the four paths that could manufacture a false verified (null verifies, unconfigured
verdict verifies, expiry ignored, two-eyes accepts one reviewer twice). All four fail the suite.

## Honest limitation

We ship **no provider endpoints**. `docs/contracts/verification-profiles.md` gives templates shaped
like the real thing, clearly marked as needing the customer's own contract. A customer with real
Nafath or Wathq credentials wires them without touching this repo.

Ships with Phase 41 as **v0.7.0**.
