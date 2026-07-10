# Phase 12 — authz-service (detailed steps)

> **Goal:** the last Tier-1 service — **authz-service** (SaaS checklist §1
> Authorization). The platform's **Policy Decision Point** (PDP) + **Policy
> Administration Point** (PAP): manage **roles → capabilities** (RBAC), evaluate
> **attribute policies** (ABAC) with a **safe structured-condition evaluator**
> (JSON rules, **no `eval`** → no policy injection), resolve a subject's effective
> capabilities, and answer *"can subject X do action Y on resource Z?"* via
> `/internal/authz/check`. Pluggable engine, config-driven, **zero new deps**.
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §1._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 12)

- [ ] `services/authz-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Pluggable `PolicyEngine`** (rbac / abac / combined) chosen by config; **allowlist, fail-fast**.
- [ ] **RBAC** — roles carry capabilities (with **wildcards**, e.g. `files.*`); a subject's **effective capabilities** = direct caps ∪ caps from assigned roles; a check allows if the required capability is held.
- [ ] **ABAC** — policies `{effect: allow|deny, action, resource, condition, priority}` evaluated by a **safe structured evaluator** (all/any/not + attr/op/value, `$`-refs to context) — **no code execution**. **Deny overrides** allow.
- [ ] **PDP** — `POST /internal/authz/check {subject, action, resource, roles?, caps?, attributes?}` → `{allow, reason, matched_policy}`; `GET /internal/authz/effective` → `{capabilities}`.
- [ ] **PAP** — role / policy / assignment CRUD (platform-admin cross-tenant, tenant-admin own-tenant).
- [ ] **Security:** condition evaluator cannot execute code (no SSTI/injection), platform cap for cross-tenant, tenant-scoped, deny-by-default, safe errors.
- [ ] **Zero new deps · zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot proven (define role+policy → check allow/deny → effective caps).

---

## 1. Prerequisites & decisions

1. **Zero new deps.** Policies are structured JSON; evaluation is a small pure interpreter (stdlib only). Storage is the `DataStore` port.
2. **Safe conditions, not code.** ABAC conditions are a restricted JSON DSL (`all`/`any`/`not`, `{attr, op, value}`) evaluated against a flat context dict. Values prefixed `$` are references resolved from context. **No `eval`, no expressions** → immune to policy injection (CWE-94/95).
3. **Deny-overrides** by default: any matching `deny` beats any `allow`; ties broken by `priority`.
4. **The PDP is the product.** The gateway and services call `/internal/authz/check`; identity can call `/internal/authz/effective` to embed caps in tokens.

---

## 2. Steps

### Step 12.1 — Generate skeleton
- `python scripts/create_service.py authz-service --desc "Authorization: RBAC + ABAC policy engine"`.
- No new deps → verify reuses the template venv. Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 12.2 — Config (zero-hardcoded)
- `POLICY_ENGINE_DRIVER` (`combined`), `DENY_OVERRIDES` (true), `WILDCARD_CAPS` (true), `AUTHZ_LIST_LIMIT` (100). Update `.env.example`.
- **Check:** boots with defaults; fail-fast on bad values.

### Step 12.3 — Ports
- `app/ports/policy_engine.py` (`decide(ctx) -> Decision`) where `ctx` = `{subject, action, resource, effective_caps, attributes, policies}`; `Decision` = `{allow, reason, matched_policy}`.
- **Check:** import pulls no third-party driver.

### Step 12.4 — Adapters + safe evaluator + registry
- `app/core/conditions.py` — the safe structured-condition evaluator (all/any/not, ops: eq/ne/in/nin/gt/ge/lt/le/contains/startswith/exists; `$`-ref resolution).
- Engines: `rbac.py` (capability check with wildcard match), `abac.py` (evaluate policies via conditions, deny-overrides), `combined.py` (RBAC allow ∪ ABAC allow, ABAC deny wins).
- Extend `container.py` with `policy_engine` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; wildcard cap matches; condition evaluator returns correct bool; a code-y condition value is treated as a literal (no execution).

### Step 12.5 — Domain: models + stores
- `core/models.py` `Role` (id, tenant, name, capabilities[], description), `Policy` (id, tenant, effect, action, resource, condition, priority), `Assignment` (subject, tenant, roles[]).
- Stores over `DataStore`: `RoleStore`, `PolicyStore`, `AssignmentStore`; plus `effective_caps(subject, tenant, extra_caps)` = direct ∪ role caps.
- **Check:** CRUD + effective-caps resolution from roles.

### Step 12.6 — Core AuthzService
- `core/authz_service.py`: role/policy/assignment CRUD; `effective(subject, tenant, extra_caps)`; `check(subject, action, resource, tenant, extra_caps, attributes)` → gather effective caps + tenant policies → `PolicyEngine.decide` → Decision.
- **Check:** unit: rbac allow/deny + wildcard, abac allow/deny + deny-overrides, effective caps, combined.

### Step 12.7 — Routes
- PAP (admin): `POST/GET /api/v1/roles`, `GET/PUT/DELETE /api/v1/roles/{id}`, same for `/api/v1/policies`, `PUT/GET /api/v1/subjects/{sub}/roles` (`platform.authz` cross-tenant / `authz.manage` own-tenant).
- PDP (internal): `POST /internal/authz/check`, `GET /internal/authz/effective` (X-Internal-Token).
- Wire `rate_limit`, deny-by-default, `guard.step("respond")`.
- **Check:** cross-tenant needs platform cap; tenant-admin scoped; PDP behind token.

### Step 12.8 — Manifest & wiring
- Manifest caps (`platform.authz`, `authz.manage`) + endpoints; port listed (policy_engine driver).
- **Check:** `/internal/manifest` lists authz port + endpoints.

### Step 12.9 — Tests
- Unit: condition evaluator (each op, `$`-refs, nested all/any/not, injection-as-literal), rbac wildcard, abac deny-overrides, effective caps, combined.
- Integration: create role → assign → `/internal/authz/effective` returns caps → `/internal/authz/check` allow; create deny policy → check denies; role/policy CRUD (cap-gated); tenant scoping.
- Security: evaluator never executes code, PDP token required, cross-tenant isolation, deny-by-default.
- **Check:** all green.

### Step 12.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: create role `editor` (caps `docs.*`) → assign to `u1` → `check(u1, "docs.edit")` = allow; add deny policy for `resource.locked == true` → `check(..., attributes={resource.locked:true})` = deny.
- **Check:** end-to-end decisions correct.

### Step 12.11 — Docs
- Service `README.md`: RBAC + ABAC model, condition DSL, PDP/PAP endpoints, plug-and-play matrix, safety.
- Update `docs/architecture-plan.md` (authz ✅ — Tier-1 complete) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **Casbin / OPA engine adapters** → future `PolicyEngine` adapters (same port).
- **Policy simulation / what-if UI** → UI is the consumer's job.
- **Time-based / geo attributes** → callers pass them in `attributes`; more built-in resolvers later.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #120 | 12.1 Skeleton | ✅ completed |
| #121 | 12.2 Config | ✅ completed |
| #122 | 12.3 Ports | ✅ completed |
| #123 | 12.4 Adapters + safe evaluator + registry | ✅ completed |
| #124 | 12.5 Domain models + stores | ✅ completed |
| #125 | 12.6 Core AuthzService | ✅ completed |
| #126 | 12.7 Routes | ✅ completed |
| #127 | 12.8 Manifest & wiring | ✅ completed |
| #128 | 12.9 Tests | ✅ completed |
| #129 | 12.10 Verify + live boot | ✅ completed |
| #130 | 12.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (134 tests; gate PASS; RBAC+ABAC live) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play + smart

| Choice | Knob | Options |
|---|---|---|
| Decision model | `POLICY_ENGINE_DRIVER` | rbac · abac · combined |
| Deny semantics | `DENY_OVERRIDES` | true · false |
| Wildcard caps | `WILDCARD_CAPS` | true · false |
| Storage | `DATASTORE_DRIVER` | memory · postgres |

**One PDP for the whole platform:** the gateway or any service asks `check(subject,
action, resource)` and gets a yes/no with a reason — RBAC and ABAC combined, decided
by **safe, structured policy** (never executed code). Add casbin/OPA later behind the
same port with no caller change.

_When every box is ✅, authz-service is live — and Tier-1 is complete._
