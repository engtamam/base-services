# Phase 52 — self-service erasure and tenant close

The compliance product sells the data-subject's erasure right, but base-services has no way for a
subject to *exercise* it: only an operator, through an internal or platform route, can erase a user
or close a tenant. A right that only staff can execute is not self-service — it is a support
ticket. This phase adds the three routes that let the subject and the owner act for themselves,
built as **thin, token-bound wrappers over the erasure primitives that already exist**.

## The belief this phase is built to destroy

> "Erasure is an operator action. To let a user erase themselves you point the same route at their
> id and check a capability."

Both halves are wrong. Pointing an existing route at an id read from the **body** is CWE-639/IDOR:
a caller erases anyone by typing their id. And a bare capability is not the guard — the guard is
that **subject and tenant come from the caller's own token, never from a request field**, plus the
structural checks (sole owner, last membership) that stop a self-service action from stranding a
tenant or destroying a person who still belongs somewhere else.

## What already exists (so we wrap it, not rebuild it)

- **Compliance** already serves the full DSAR flow (`/api/v1/compliance/dsar` + `verify` +
  `process`), and audits every erasure against a **pseudonymised `subject_ref`** — an HMAC with no
  inverse, so the audit is provable AND erasable (T2).
- **Identity** has internal `erase_subject`, membership `revoke` (idempotent), and `/api/v1/auth/me`.
  It owns the membership count, so "does this person belong anywhere else" is a local question.
- **Tenant** has internal `erase_subject`, `core/erasure`, and the `tenant.manage` capability.

Every route below binds identity to the token and delegates the actual work to one of these.

## The work

### 52.1 — `POST /api/v1/compliance/me/dsar` (`compliance.self`)

- The self-service shape the compliance **manifest already names**. Subject and tenant come from
  the caller's token; the body carries only the request *kind* (access / erasure), never an id.
- Delegates to the existing DSAR machinery — this is the *erase-me-everywhere* orchestration that
  fans out to each service's internal `erase_subject`. It is the comprehensive path; §52.2 is the
  identity-local one, and §52.5 draws the line between them so they never double-erase.
- Grace as config: an erasure DSAR schedules with a recovery window, audited immediately as the
  subject.

### 52.2 — `DELETE /api/v1/auth/me` (`identity.self`)

- Revoke THIS caller's membership in the token's tenant; when it was the person's **last**
  membership anywhere, delete the Person too. A person who still belongs to another tenant is never
  touched — T2/CWE-640: an existing person's identity is not collateral.
- Idempotent: revoking an already-gone membership is success (a resumed client re-attempt is not an
  error).
- **Guarded by the sole-owner check (§52.4):** the last owner of a live tenant cannot delete their
  own membership out from under it — that would strand the tenant exactly as a close would. Refuse
  with a clear "close the tenant first" rather than orphaning it.

### 52.3 — `POST /api/v1/tenant/close` (`tenant.manage`)

- Owner self-close of the token's tenant. Schedules closure with **grace as config** (a wound-down
  tenant is recoverable within the window, then erased), audited immediately as the subject.
- Fail-closed and idempotent: closing an already-closing/closed tenant is success; a close that
  cannot be audited does not happen (the platform's standing rule).

### 52.4 — the sole-owner / last-owner guard (shared)

- One guard, used by BOTH §52.2 and §52.3: an action that would leave a live tenant with **no
  owner** is refused. For close, that is the owner asking to close; for `DELETE /auth/me`, it is the
  last owner revoking themselves. The guard is structural, not a capability — a second owner makes
  either action allowed.
- Grace window lives in per-service config, read at the point of use (no inert knob), and is a
  RUNTIME_POLICY key so an operator can tune the recovery window per tenant.

### 52.5 — the DSAR ↔ /auth/me boundary

- Define, in code and doc, which route owns the cross-service cascade. `/compliance/me/dsar` is the
  comprehensive erase-everywhere orchestration; `/auth/me` is the identity-local membership/person
  delete. `/auth/me` MUST NOT re-run the full compliance fan-out, and the compliance erasure MUST
  NOT assume identity already deleted the person — each is complete on its own seam, and running
  both produces one erasure, not two half-erasures.

## Definition of Done

- Three routes, each taking subject and tenant from the **token**, never a body id; the wrong-id
  attempt is structurally impossible, and a test proves a body id is ignored.
- Sole-owner / last-owner guard refuses any action that would leave a live tenant ownerless, on both
  `DELETE /auth/me` and `tenant/close`.
- Grace is config (per-service, RUNTIME_POLICY, read at use); erasure/close schedule with a recovery
  window and audit **immediately** as the pseudonymised subject.
- `/auth/me` deletes the Person only on the last membership; a person who belongs elsewhere is
  untouched. Idempotent re-close / re-revoke is success.
- The DSAR ↔ /auth/me boundary is explicit; the two paths never double-erase.
- Non-functionals: each new capability (`compliance.self`, `identity.self`) is in its service's
  manifest and enforced; token-bound, rate-limited; audit fail-closed; gates + full suites green.

## Tasks

- [ ] **52.1** `POST /api/v1/compliance/me/dsar` (`compliance.self`) — token-bound self DSAR over the
  existing flow; body carries kind, never an id; grace as config; audit as subject.
- [ ] **52.2** `DELETE /api/v1/auth/me` (`identity.self`) — revoke this membership, delete the Person
  on the last one (T2-safe), idempotent, behind the sole-owner guard.
- [ ] **52.3** `POST /api/v1/tenant/close` (`tenant.manage`) — owner self-close, grace as config,
  fail-closed, idempotent.
- [ ] **52.4** Shared sole-owner / last-owner guard + grace config (RUNTIME_POLICY, read at use),
  used by 52.2 and 52.3.
- [ ] **52.5** Draw the DSAR ↔ /auth/me boundary in code + doc; a test proves the two never
  double-erase.
- [ ] **52.6** Mutation tests: body-id ignored (IDOR refused), sole-owner refused, last-membership
  cascade vs person-kept, grace scheduling + immediate audit, idempotent re-close/re-revoke.
