# Phase 31 — One person, many tenants

A freelancer works for two companies. A consultant has five clients. Today they need two logins,
two passwords, two of everything — because a `User` is `(tenant, username)`, so the same human in
two tenants is two unrelated accounts.

They want one login. They must **never** get one view. Company A seeing Company B's work is a leak
and probably a contract breach — so the tenant wall does not move a millimetre. The person simply
gets a key to two doors, and can only ever stand in one room at a time.

---

## The invariant that makes this cheap

**A token carries exactly ONE tenant. Never two. Ever.**

Identity already mints `claims = {"caps": …, "roles": …, "tenant": user.tenant}`, and all sixteen
other services read the tenant from that claim and scope everything to it. If the token stays
single-tenant, **not one of those services changes** — the freelancer is simply two sessions.

That is the whole design. Everything below exists to protect that one sentence.

```
Person      one login, global          (identifier, password, MFA)
Membership  person × tenant × roles    (the key to one door)
Token       person + EXACTLY ONE tenant
```

Login authenticates the **person** and returns *only their list of memberships* — no data, no
tenant token yet. Choosing a tenant mints a token scoped to it. Switching tenants mints a new one.

---

## 1. Definition of Done

### (1) Is this on the hot path?

Token *verification* is the hot path, and it does not change: a service still reads one `tenant`
claim. **Zero new reads in sixteen services** — a test asserts the token shape is unchanged.

What is new is on the *mint* path (login, tenant-switch, refresh): a membership lookup. It is a
keyed `get` on `(person, tenant)` — O(1), no scan. It must not become a `find` over memberships;
the `safety:scans` gate already refuses that.

### (2) Does every config knob have a consumer?

`IDENTITY_MULTI_TENANT` (default **off**). Off, a single-tenant product behaves exactly as today —
this is not a migration anybody is forced into. The knob-guard test enforces the consumer.

### (3) What production needs that "add a memberships table" omits

* **Revocation must bite at REFRESH, not at TTL.** Remove a contractor from a tenant and their
  access token stays valid for its lifetime — that is acceptable and bounded. But the *refresh*
  token must not silently mint a new one. Refresh re-verifies the membership still exists. Today
  the refresh row stores only `user_id` — **no tenant** — so it would happily re-mint for a tenant
  the person was fired from. That is the single most dangerous line in this phase.
* **Logging out of one tenant must not log you out of all.** Or it must, deliberately — but it
  must be a decision, not an accident of how the refresh family is keyed.
* **Audit must say which tenant a person acted in.** It already does: `subject_ref` is salted per
  tenant, so the same human has a different pseudonym in each. Cross-tenant correlation of one
  human remains impossible — which is exactly what we want, and it is already built.

### (4) The correctness footgun

**Minting a token for a tenant the person is not a member of.**

Sixteen services trust the `tenant` claim absolutely. They have to — that trust is what makes the
architecture work. The *only* place it is ever checked is the moment the token is minted. If that
check is missing, weak, or bypassable, it is not a bug in identity-service; it is a total
cross-tenant breach of the entire platform, and every other gate we built would still be green.

So the check is not a line in a function. It is a **gate** (`safety:tenancy`) and it is
mutation-tested: no token may carry more than one tenant, and no token may be minted for a tenant
without a verified, current membership.

**The second footgun, which is quieter and worse.** Turning `IDENTITY_MULTI_TENANT` on changes the
uniqueness scope of the login identifier from **per-tenant** to **global**. Today two tenants may
each have an `alice@x.com` — and those may be *two different humans*. Merging them into one Person
because the strings match is a catastrophic identity confusion: one human silently gains access to
the other's employer. The migration must **detect the collision and refuse**, never merge. A test
will assert exactly that.

---

## 2. Steps

| # | Step |
| --- | --- |
| 31.1 | `Person` + `Membership` models; `MembershipStore` keyed `(person, tenant)` — O(1), no scan |
| 31.2 | `IDENTITY_MULTI_TENANT` knob (default off); single-tenant path byte-identical to today |
| 31.3 | Login returns memberships ONLY — no data, no tenant token |
| 31.4 | `POST /auth/tenant` — verify membership, mint a token scoped to ONE tenant |
| 31.5 | Refresh token carries its tenant AND re-verifies membership; a revoked membership fails refresh |
| 31.6 | **GATE `safety:tenancy`** — no token carries >1 tenant; no mint without a current membership. Mutation-tested |
| 31.7 | Migration: existing `User(tenant, …)` → `Person` + one `Membership`. **Collision detection: two different humans sharing an identifier across tenants must REFUSE, never merge** |
| 31.8 | Tests: switching tenants cannot see the other's data; a removed member fails refresh; the token shape is unchanged so no other service is touched |

## 3. Out of scope

**A tenant that owns other tenants** (agency, reseller, holding company). That is a *hierarchy*,
and it deliberately punches a hole in tenant isolation so a parent can look inside its children.
It is a different phase, a different threat model, and it is where companies get breached. It is
not this.

## 4. What this design refuses to do

There is no token, and no endpoint, that returns data from two tenants at once. Not for admins,
not for support, not for the freelancer's own convenience. The moment such a thing exists, every
service's tenant scoping becomes advisory, and the wall is decoration.

---

## 5. Threat model — this is the feature where one mistake breaches every tenant

Sixteen services scope everything to the `tenant` claim, and they are *right* to trust it — that
trust is what makes the architecture work. Which means **the mint is the only door, and it is now
a door with several locks that did not exist before.** Every item below is a real way in.

### T1 — Mint a token for a tenant you are not a member of  (CWE-639, CWE-863) — CRITICAL

`POST /auth/tenant {"tenant": "victim-corp"}`. If the membership is not verified, the attacker
gets a fully valid token and **every other gate in this repo stays green** while they read a
competitor's invoices. This is not an identity bug; it is a total platform breach.

*Mitigation:* membership is verified on **every** mint, keyed `(person, tenant)`, and the tenant in
the token is **never** taken from the request — it is taken from the membership row that was found.
Gated by `safety:tenancy`, mutation-tested: delete the check, the gate must go red.

### T2 — A tenant admin resets the person's password and takes their OTHER clients  (CWE-640, CWE-620) — CRITICAL, and the one multi-tenancy *creates*

This vulnerability **does not exist today** and is the easiest to miss. Today a tenant admin
managing "their" users is harmless: those users belong to that tenant alone. The moment a `Person`
is global, a tenant admin who can reset that person's password owns the freelancer's access to
**every other company they work for.** Company A's admin — or anyone who social-engineers A's
helpdesk — walks into Company B.

*Mitigation, and it is a hard boundary:* a tenant admin may create, suspend and remove a
**Membership**. A tenant admin may **never** touch the `Person` — not the password, not the MFA,
not the login identifier, not deletion. Credential recovery belongs to the person alone (email
proof of control). This is enforced by capability, and the gate asserts no tenant-scoped capability
reaches a Person-credential route.

### T3 — Caps taken from the Person instead of the Membership  (CWE-269) — CRITICAL

The freelancer is an *admin* at their own tiny company and a *read-only contractor* at the bank.
If `caps` are stored on the Person, they are admin at the bank.

*Mitigation:* `caps`/`roles` live **only** on the Membership. The Person carries **zero**
authority. A test asserts a Person object has no caps field at all — make it unrepresentable.

### T4 — Refresh re-mints after the membership is revoked  (CWE-613) — HIGH

Today the refresh row stores `user_id` and **no tenant**. Fire a contractor and refresh happily
mints them a new access token for the tenant they were removed from, for the whole refresh
lifetime.

*Mitigation:* the refresh row carries its tenant, and refresh **re-verifies the membership is still
current**. Revocation bites at refresh, not at TTL. A suspended tenant or suspended membership
fails refresh too.

### T5 — Lockout amplification  (CWE-645, DoS) — HIGH, and easy to overlook

Login is now against a **global Person**. An attacker who spams bad passwords for
`consultant@x.com` locks them out of **all five of their clients at once**. We turned a per-tenant
nuisance into a way to remove a consultant from their entire livelihood from an unauthenticated
endpoint.

*Mitigation:* lockout counts per `(person, source)` with the existing full-jitter backoff, and a
lockout **never** blocks a session that is already authenticated for another tenant. Failing into a
total denial-of-service is not "secure by default"; it is a different outage.

### T6 — Membership enumeration  (CWE-204, CWE-208) — MEDIUM

`POST /auth/tenant {tenant: X}` answering *403 not a member* vs *404 no such tenant* tells an
attacker who works where. That is commercially sensitive on its own — a competitor learns your
client list.

*Mitigation:* one uniform response, constant-time comparison, no distinction between "no such
tenant" and "not your tenant". Same discipline as the existing no-enumeration login.

### T7 — Two humans, one email  (identity confusion) — CRITICAL at migration

Covered above and repeated here because it is the quiet one: today `alice@x.com` may exist in two
tenants as **two different people**. Merging them because the strings match hands one human the
other's employer.

*Mitigation:* the migration **detects the collision and refuses**. It never merges. A refusal is an
operator's afternoon; a merge is a breach nobody notices.

### T8 — A token with no tenant claim  (fail-open) — HIGH

`_tenant()` in analytics reads `p.tenant or "global"` for a platform caller. If a token ever
arrives *without* a tenant claim, "no tenant" must never widen into "all tenants".

*Mitigation:* a missing tenant claim is a **rejected token**, not a wildcard. Fail closed, and a
test asserts it.

### T9 — Zero memberships, suspended tenant, suspended membership — MEDIUM

A person with no memberships must receive **no tenant token at all** — not an empty one, not a
"global" one. A suspended tenant or membership must refuse the mint, not merely hide the data.

### T10 — Step-up on switch — MEDIUM (design decision, stated not defaulted)

Switching from a low-trust tenant to a high-trust one on an existing session widens blast radius
if that session is stolen. `IDENTITY_TENANT_SWITCH_REAUTH` (default off) can require MFA re-proof
on switch. Off by default is a choice, and it is written down rather than assumed.

## 6. The gate: `safety:tenancy`

Not a line in a function — the check that stands between every company on the platform gets a gate,
and it is mutation-tested (delete the check → the gate must go red):

1. No token may carry more than one tenant.
2. No token may be minted for a tenant without a **current, verified** membership.
3. The tenant written into the token comes from the **membership row**, never from the request.
4. No tenant-scoped capability may reach a `Person`-credential route (T2).
5. A `Person` model may not carry `caps` or `roles` (T3) — unrepresentable, not merely unused.

---

## 7. What was built

| Threat | Where it is stopped | Proven by |
| --- | --- | --- |
| T1 mint for a tenant you lack | `core/tenancy.py` — `claims_for()` has **no tenant parameter**; the claim is read off the membership row | 3 unit + 1 API test, gate R1/R3 |
| T2 admin resets the person's password | there is **no route** that mutates a `Person` — the gate fails any tenant-scoped route that writes one | gate R4, mutation-tested |
| T3 caps on the person | `Person` has no `caps`/`roles` **field** | `dataclasses.fields(Person)` test, gate R5 |
| T4 refresh after revocation | the refresh row carries its tenant; `refresh()` re-runs `authorize_tenant()` | 5 unit tests, gate R2 |
| T5 lockout amplification | counter keyed `(source, person)` — one attacker cannot unemploy a consultant | 2 unit tests |
| T6 membership enumeration | one `TenantAccessDenied` with an **empty message**; 403 either way | unit + API test comparing bodies |
| T7 two humans, one email | `create_person()` **refuses** on a taken identifier | unit test (and the loser is not half-written) |
| T8 tenantless token | `tenant_of()` raises — never widens to "all" | unit test over 4 shapes |
| T9 no membership → no token | `authorize_tenant()` refuses; nothing is signed | 4 unit tests asserting `issuer.minted == []` |
| T10 step-up on switch | `IDENTITY_TENANT_SWITCH_REAUTH`, default off | API test both ways |

**Three things the checklist did not ask for, and production does.**

1. **The two-step login.** `POST /auth/person/login` returns the person's doors and **no token** — there
   is nothing for a token to be scoped to until a tenant is chosen, and a tenant-less token is
   precisely the thing sixteen services cannot check (T8). So we do not create one, not even
   briefly.
2. **The signer owns its claims.** `payload.update(extra)` in the JWT encoder let a caller
   overwrite `sub`, `type` and `exp`. Nothing exploited it — it was a loaded gun on the one path
   where a forged claim is invisible downstream. Reserved claims now raise instead of merging.
3. **Refresh fails closed when the store is gone.** Turning `IDENTITY_MULTI_TENANT` off under a
   live session must not silently un-revoke every fired contractor. A tenant-scoped refresh row
   with no membership store to check it against is refused, not waved through.

**The gate is mutation-tested.** `tools/ci/selftest_tenancy.py` writes a correct service, confirms
it passes, then breaks it once per rule — the tenant taken from the request, two tenants in one
claim, a guard with no `raise`, a refresh that stops re-verifying, an admin route resetting a
password, caps on the person — and demands the gate go red for each. All six mutations are caught.
A rule whose mutation survives is not enforced, and the green is a lie.

**STATUS: ✅ BUILT.** 224 identity tests pass (19 skipped); `safety:tenancy` PROVEN able to fail.
