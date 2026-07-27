# Phase 48 — founder seed: bootstrapping a tenant's first owner

A single new internal endpoint on identity-service, `POST /internal/tenancy/seed-owner`, and the
wiring that lets self-service onboarding stand up a tenant with a real human owner — as a `Person`,
not a legacy `User`.

## The belief this phase is built to destroy

> "A new tenant can add its own first admin."

It cannot. In multi-tenant mode, membership mutations require `members:write`, and a freshly
provisioned tenant has **nobody** — so nobody holds the capability to add the first admin. The one
route that creates an account, `/auth/register`, mints a legacy single-tenant `User`, not a
`Person`, so it does not even produce the right kind of identity. The result is a chicken-and-egg:
the tenant exists, and no human can ever enter it. Every self-service signup dead-ends here.

## The one door that breaks the egg

The missing piece is small and specific: a **service-to-service** door that grants the *first* owner
without a pre-existing `members:write` holder. Not a user route — a user route would need the very
capability that does not yet exist — but an internal-token route the provisioning flow calls.

Almost everything it needs already exists and is already correct. `IdentityService.add_member`
already: creates-or-reuses a `Person` by identifier, grants a `Membership`, and holds **THE HARD
BOUNDARY (T2, CWE-640)** — *if the person already exists, it grants a membership and touches nothing
else: not their password, not their MFA, not their identifier.* A freelancer being made owner of a
new company already works for two others; their credential is the key to those companies too, and
this platform has no claim on it. So `seed-owner` is ~90% a thin, internal-token wrapper over tested
code — plus two guards that make it a *bootstrap* primitive rather than a general grant backdoor.

## What makes it powerful, and every edge case it must survive

The value is not the happy path (create a person, grant owner) — it is that the happy path is the
*least* of it. The endpoint is idempotent, credential-safe, race-safe, and fail-closed audited.

| edge case | what must happen |
|---|---|
| **idempotent retry** — a provisioning saga re-runs, or the caller times out and retries | seeding the same identifier as owner again is a NO-OP that returns the existing membership (`seeded:false`). `MembershipStore.grant` is an upsert; re-granting changes nothing. A retry must never create a second owner or a second invite. |
| **the person already exists** (one human, many orgs) | reuse the existing `Person`; grant the membership; touch NOTHING else. A `password` supplied for an existing person is REFUSED (`CredentialNotYours`, 403) — an admin seeding a tenant has no claim on a human's existing credential (T2). |
| **a brand-new person** | with a `password`, they can log in immediately; without one, a single-use **invite token** is issued and returned exactly once — the human sets their own password, the caller never knows it. |
| **the egg is not actually broken** | REFUSE if the requested `caps` do not include `members:write`. A "seeded owner" who cannot add anyone has not solved the bootstrap — it would hand back a tenant still unenterable, silently. |
| **a second, different owner** | `only_if_empty:true` (default) REFUSES seeding a *different* new owner when the tenant already has members — so seed-owner cannot be an accidental or malicious backdoor to a second owner on a live tenant. Re-seeding the SAME owner stays a no-op, so retries still work. |
| **two provisioning calls race** | the `create_person` loser catches `PersonExists`, re-reads by identifier, and proceeds against the winner's `Person`. The membership grant is idempotent, so both converge on one owner. |
| **the identifier belongs to a DIFFERENT human (T7)** | `person_by_identifier` resolves the right human and reuses them; two different humans who share an email are NEVER merged — that would hand one the other's company. |
| **single-tenant mode** | when `IDENTITY_MULTI_TENANT` is off there is no membership store; the route refuses (503 `MULTI_TENANT_OFF`), exactly like the other tenancy routes, rather than half-acting. |
| **the record cannot be written** | the action is audited WRITE-AHEAD and CRITICAL; if the log cannot take it, the grant does not happen (503). There is no such thing as an owner nobody can account for. |

## Linked, and legally sound

Two properties this phase must guarantee, because an owner grant is the most consequential thing
identity-service does:

- **Linked to the human.** The owner is a `Person` (one global identity) joined to the tenant by a
  `Membership` row keyed on `person_id`. Reuse means the SAME `person_id` — the link is permanent and
  unambiguous: *this human owns this tenant*, across every organisation they belong to.
- **Provable AND erasable.** `tenant.owner_seeded` records who (`person_id`) became owner of which
  tenant, with which roles/caps, when, and via which flow — never the credential (T2). The audit
  *actor* is a **pseudonym** (`subject_ref`), not a raw name or email: a hash-chained log holding raw
  identities becomes a PII store that cannot honour an erasure request (deleting a row breaks the
  chain). The pseudonym keeps the trail **provable** — resolve `subject_ref` → `Person` inside
  identity-service — while staying **erasable** for GDPR/PDPL. Provable in court, compliant with
  erasure law: both, not either.

## The contract

```http
POST /internal/tenancy/seed-owner      X-Internal-Token: …
{
  "tenant":        "t_123",
  "identifier":    "founder@acme.com",
  "roles":         ["owner"],                 # membership roles in THIS tenant
  "caps":          ["members:write", …],      # MUST include members:write
  "password":      "…" | null,                # brand-new Person only; refused for an existing one
  "only_if_empty": true                       # refuse a different owner if the tenant is staffed
}
→ 200 (already an owner) | 201 (seeded)
{
  "person_id":     "p_…",
  "tenant":        "t_123",
  "created_person": true|false,               # was the Person newly created?
  "seeded":         true|false,               # did we grant, or was it already so?
  "invite_token":   "…" | null,               # returned ONCE, for a brand-new invite flow
  "roles":          [...], "caps": [...]
}
```

## Wiring it to be rich (optional, event-driven, never load-bearing)

The endpoint is useful alone; wired, it becomes the hub that lights up onboarding. Everything below
is at-most-once *hint* wiring — the durable fact is always the `Membership` row, never the event
(the same rule as billing/wallet: an event is a hint, never the only way a fact is learned).

- **onboarding-service (the primary caller).** Add a `seed_owner` step to the provisioning saga:
  after the tenant exists, seed its founder. The saga's COMPENSATION revokes the membership if a
  later step fails — reusing the resume/undo machinery from 47.2. This closes the self-service loop
  end to end.
- **tenant-service.** `POST /tenants` may seed the owner in the same flow, passing the founder's
  identifier, so "create tenant" and "give it an owner" are one operation to the caller.
- **notification-service.** On a brand-new-person *invite*, send the welcome/invite email — either
  onboarding orchestrates it, or identity emits `tenant.owner_seeded` and notification consumes it.
- **billing-service.** Consume the same event to start the tenant's trial/subscription — founder
  seeded ⇒ the clock starts.
- **authz-service — deliberately decoupled.** identity owns *membership* roles/caps; if the owner
  needs *platform* capabilities, the emitted event lets authz grant them, rather than identity
  reaching across a boundary it does not own.

Whether to emit `tenant.owner_seeded` is the one real design choice: emitting it is what turns a
plumbing fix into that hub. Default this phase to **emitting it** (declared in the manifest, so a
consumer can discover it), while keeping every consumer optional.

## Definition of Done

- `POST /internal/tenancy/seed-owner` exists, internal-token gated, and behaves per the edge-case
  table — proven by a test per row (idempotent retry, existing-person credential refusal, invite vs
  password, missing-`members:write` refusal, `only_if_empty`, race, single-tenant-off, audit-fail).
- Audited write-ahead as CRITICAL (`tenant.owner_seeded`), actor pseudonymised; declared in
  `auditable_actions.py`. The credential never appears in the record.
- Declared in the manifest `endpoints`; if the event is emitted, declared in manifest `events.emitted`
  and covered by `safety:event-contract`.
- Non-functionals: no new unwired config knob (any TTL/mode reuses the existing
  `IDENTITY_INVITE_TTL_SECONDS` / `IDENTITY_MEMBER_ADD_MODE`); the route is rate-limited/allow-listed
  like the other internal routes; the reused `find`/`grant` stay off any scan path (`safety:scans`).
- Gates green: `safety:manifest-routes`, `safety:audit-wiring`, `safety:openapi`,
  `safety:event-contract` (if emitting), plus the full suite.
- Onboarding wiring is a SEPARATE, optional task (48.2) so the identity primitive can ship and be
  verified on its own first.

## Tasks

- [x] **48.1** identity-service: `seed_owner` service method (wrapping `add_member` with the
  `members:write` guard and `only_if_empty` bootstrap check) + `POST /internal/tenancy/seed-owner`,
  audited WRITE-AHEAD + CRITICAL as `tenant.owner_seeded` via an `on_commit` callback (fired only after
  the guards pass, before the grant), actor pseudonymised. Declared in manifest + auditable actions.
  A test per edge-case row (8).
- [x] **48.2** identity + onboarding: made seed-owner orchestration-safe. seed-owner returns an `id`
  (`tenant::person_id`) undo reference, and a new `DELETE /internal/tenancy/membership/{ref}` revokes
  one membership idempotently (never the Person; T2) — the compensation a saga calls so a failed
  signup leaves no orphan owner. Onboarding's provisioner is deploy-config (`ONBOARDING_PROVISIONERS =
  tenant_owner=<seed-url>|<revoke-url>`); a contract test proves its generic HttpProvisioner speaks the
  seed-owner/revoke shapes (captures the id, DELETEs it, 404 = success, no-id = fail-loud).
- [x] **48.3** `tenant.owner_seeded` domain event: emitted best-effort (at-most-once HINT) on a real
  seed — not on an idempotent no-op re-seed, and a failing bus never fails a committed seed — carrying
  `tenant`/`person_id`/`created_person`/`roles` (no raw identifier). Declared in manifest
  `events.emitted`; `safety:event-contract` green. A live consumer stays opt-in by design: the durable
  fact is the membership row + audit, so any team can subscribe (notification welcome, billing
  trial-start) without the seed depending on it.

---

**Phase 48 complete.** The chicken-and-egg is broken: self-service onboarding can bootstrap a tenant
owner as a `Person` over the internal token — idempotent, T2-safe, fail-closed audited, orchestration-
safe (undo reference + revoke), and announced on the bus for anyone who wants to react. identity 391
tests green; the seed is the one door, guarded so it stays a bootstrap and not a backdoor.
