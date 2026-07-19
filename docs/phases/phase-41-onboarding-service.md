# Phase 41 — onboarding-service

## The idea

**Onboarding is not a wizard.** It is a set of REQUIREMENTS evaluated against the world as it is
right now. Nobody is "on step 3 of 7" — they have some requirements satisfied and some not.

That is not a stylistic preference. A wizard breaks on the first real case, and every break becomes
skip-logic:

- a user INVITED to an existing org must not see "create an organisation"
- a seller needs KYC; a buyer does not
- billing is connected in billing-service, not by this wizard
- someone abandons halfway and returns three weeks later
- a business in KSA needs a CR number a B2C user never touches

Requirements handle all five without a branch. The invited user has **already satisfied** "belongs
to an org" — there is nothing to skip. Resuming is free because state is recomputed rather than
remembered. Reordering is free because `blocks` expresses dependency, not sequence.

## Decisions

**`satisfied_by` names who owns the truth.** `identity.email_verified` means identity-service
decides and we ask. Onboarding never keeps a second copy of another service's fact, because a copy
drifts and the drift always favours "satisfied" — it is written when a fact becomes true and
nothing writes it back when the fact stops being true.

**Evaluated on read, so regression is honest.** A verification that expires makes its requirement
unsatisfied again and re-blocks what it gated. A stored tick can only move one way.

**`unknown` never counts as satisfied.** An onboarding service that treated an unreachable
identity-service as "probably fine" would hand out whatever completion grants — precisely while the
platform is unhealthy and nobody is watching. It is never `unsatisfied` either, because blocking
every customer during a blip is its own outage.

**The entitlement is a port.** wallet_credit is the GCP $300 pattern; a platform with no wallet
declares `trial_period` or `plan` and the same flow works. The flow declares WHAT is granted, config
decides HOW.

**`once_per` names a verified identity.** Both reference implementations studied could be drained
the same way: sign up, take the credit, delete the account, sign up again — their key was the tenant
or the email, and both are free to create. Ours is a verified registration whose hash survives
erasure, which is the entire reason verification-service was built first. An unverified claim of
uniqueness is REFUSED, not trusted.

**Provisioning compensates.** Signup creates a tenant, a membership, sometimes a credit — across
services onboarding does not own. What it owns is the ORDER and the obligation to put the world back.
`undo` sits on the PORT rather than being left to each caller, because a create without a matching
delete is exactly how orphans happen: nobody writes the undo at the time they write the create.

## What the process caught

Four functional holes, none found by reading the code:

- **the anti-abuse registry had no writer** — `identity_seen` was never populated, so the gift was
  farmable exactly as in the reference projects (found by the dead-code gate)
- **`mark_granted` was never called** — a grant delivered while the requirement still read
  unsatisfied, so the flow could never complete (dead-code gate)
- **a duplicated rule** — `Answer.is_satisfied` carried the reasoning for the most safety-critical
  decision here in a property nothing called, while the evaluator restated it as a bare string
  comparison. Mutating the careful copy broke no test (mutation testing)
- **identity-service reported a false erasure** — `delete_person()` had no callers, so a DSAR left
  the login, password hash and MFA secret in place while the receipt said `retained: []` (dead-code
  gate, fixed in 41.13)

And one test that passed for the wrong reason: `complete is False` held because a *different*
requirement was unsatisfied, masking `unknown` being treated as satisfied. Rewritten to isolate.

## The dead-code gate (41.11)

Tuning it honestly took three attempts, each recorded in the file:

| attempt | outcome |
|---|---|
| confidence 90 | a planted dead function walked straight past — vulture scores functions and properties at 60 |
| confidence 60, whole app | ~200 correct symbols per service; FastAPI handlers and Protocol methods have no textual caller |
| scan all, report `app/core` only | usages still visible, framework noise gone |

68 findings were then hand-triaged in an isolated worktree: 9 deleted, 3 wired, 56 allowlisted with
specific reasons. Two symbols that *looked* like safety holes were verified correct and left alone —
`is_held` and `Consent.active` restate their check inline because callers need which hold, or
withdrawn-vs-expired, which a bool cannot carry.

The triage also found a hole in the gate itself: it skipped an entire service whenever any allowlist
key *contained* that service's path, so one allowlisted file would have silently stopped scanning
the whole service.

## Honest limits

- `Saga` compensation covers what onboarding orchestrates. Steps another service takes internally
  are that service's to undo.
- The gate finds a symbol nobody uses. It cannot find the same rule written twice with one copy
  rotting — only mutation testing found that. Both are needed.
- Provisioning resume is called for a known subject, never swept across every run: resuming
  re-attempts DELETES, and a blanket sweep would re-attempt them on records an operator may be
  mid-way through inspecting.
