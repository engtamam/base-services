# authz-service

> Self-contained, plug-and-play **FastAPI** authorization service (SaaS checklist §1).
> The platform's **Policy Decision Point** (PDP) + **Policy Administration Point**
> (PAP): manage **roles → capabilities** (RBAC), evaluate **attribute policies**
> (ABAC) with a **safe structured-condition evaluator** (JSON rules, **no `eval`**),
> resolve effective capabilities, and answer *"can subject X do action Y on resource
> Z?"*. Backend only. Built from `_template`. **Zero new deps.**

---

## The decision (PDP)

`POST /internal/authz/check {subject, action, resource, tenant, extra_caps?, attributes?}`
→ `{allow, reason, matched_policy}`. The gateway or any service asks; authz answers.

- **RBAC** — a subject's **effective capabilities** = direct grants ∪ caps from
  assigned roles (wildcards: `docs.*` grants `docs.edit`). Allow if the required
  capability is held.
- **ABAC** — policies `{effect, action, resource, condition, priority}` matched by
  action/resource globs + a **condition** over request attributes. **Deny overrides**.
- **combined** (default) — allow if RBAC *or* ABAC allows; an ABAC **deny** wins over
  everything.

## Condition DSL (safe — never executed)

```json
{"all": [
  {"attr": "resource.owner", "op": "eq", "value": "$subject.id"},
  {"attr": "resource.locked", "op": "eq", "value": false}
]}
```
`all` / `any` / `not` + `{attr, op, value}`. Ops: `eq ne in nin gt ge lt le contains
startswith exists`. `$`-values are context references. It is a **pure interpreter** —
a malicious policy value like `__import__('os')` is compared as a literal, never run
(defeats policy/expression injection, CWE-94/95); unknown ops fail closed.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/authz/check` | **decide** (PDP) |
| internal | `GET /internal/authz/effective?subject=&tenant=` | effective caps (for identity to embed in tokens) |
| user | `POST/GET /api/v1/roles`, `GET/PUT/DELETE /api/v1/roles/{id}` | manage roles (PAP) |
| user | `POST/GET/DELETE /api/v1/policies` | manage ABAC policies |
| user | `PUT/GET /api/v1/subjects/{sub}/roles` | assign roles/direct caps |

PAP is `platform.authz` (cross-tenant) or `authz.manage` (own tenant, forced scope).

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Decision model | `POLICY_ENGINE_DRIVER` | rbac · abac · combined |
| Deny semantics | `DENY_OVERRIDES` | true · false |
| Wildcard caps | `WILDCARD_CAPS` | true · false |
| Storage | `DATASTORE_DRIVER` | memory · postgres |

Add a Casbin/OPA engine later behind the same `PolicyEngine` port — callers never change.

## Security

Conditions are **data, not code** (no SSTI/injection); PDP behind a timing-safe internal
token; PAP cap-gated + tenant-scoped (a role in one tenant is invisible in another);
default-deny; safe errors.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

Casbin / OPA engine adapters · policy simulation / what-if · richer built-in attribute
resolvers (time, geo — callers pass them in `attributes` today).
