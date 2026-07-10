# Phase 3 — Auth, RBAC & CWE Self-Check (detailed steps)

> **Goal:** make the user tier real and self-defending. Wire the **`TokenVerifier`**
> port into `/api/v1` (replace the Phase 1 deny-by-default stub), add **capability
> RBAC** (deny-by-default, the `caps` claim is the sole authority), add **auth rate
> limiting**, and bundle **`cwe.json` + `CweGuard`** — the before/after-step
> self-check that names the exact CWE it defends.
>
> _Reference: `docs/architecture-plan.md` §6 (security model), §9 (standards & CWE self-check)._
> _Rules that apply: self-contained · zero-hardcoded · secure-by-default · deny-by-default · runs-with-zero-infra · permissive licenses._

---

## 0. Definition of done (Phase 3)

Phase 3 is complete when **all** are true:

- [ ] `/api/v1/*` is protected by **real token verification** via `container.token_verifier`; a valid token yields a `Principal`, an invalid/expired/missing one → generic **401**.
- [ ] **Capability RBAC** is deny-by-default: `require_capability(...)` / `require_any_capability(...)` read **only** the `caps` claim; no `caps` → **403**. No role string alone grants access.
- [ ] The verified **`Principal`** (subject, caps, tenant) is available to routes and carried in `request.state` for tenant-scoping.
- [ ] **Auth rate limiting** via `container.rate_limiter` → generic **429** when exceeded.
- [ ] Every service **bundles its own `cwe.json`** (self-contained copy) + a `CweCatalog` that resolves any `CWE-###` to name/type.
- [ ] **`CweGuard`** runs **pre- and post-step** checks; the step→CWE map is **config** (`step_cwe_map.toml`, stdlib — zero-hardcoded), and a violation **fails closed** and is logged with the exact CWE id.
- [ ] Built-in checks work: **SSRF allowlist** (CWE-918), **path-traversal** (CWE-22), **output secret-scan** (CWE-200/532), **parameterization assertion** (CWE-89).
- [ ] All tests green with **zero infra**; safe generic errors (no leaks); live boot verified.

---

## 1. Prerequisites & decisions

1. **No new runtime deps.** Token verify uses the Phase 2 `TokenVerifier` (PyJWT, already present). The step→CWE map uses **`tomllib`** (Python 3.11+ **stdlib**) — chosen over YAML to avoid a dependency and shrink attack surface.
2. **`cwe.json` source** — copy `docs/cwe.json` (MITRE catalog) into `app/security/cwe.json`. Self-contained rule: the file lives *inside* the service.
3. **Generic, not domain-specific.** No `wir.*`/`platform.*` caps baked in — capabilities are arbitrary strings supplied by the token. The admin-bypass hardening is expressed generically (a configurable "no role-only escalation" rule), not with ammar cap names.
4. **CweGuard is a real engine, not theater** — pluggable pre/post checks with concrete built-ins; a service opts a step into the guard and the guard fails closed on a violated precondition.

---

## 2. Steps

### Step 3.1 — Principal + real auth dependency (`app/api/auth.py`)
- `Principal` dataclass: `subject`, `caps: frozenset[str]`, `tenant: str|None`, `raw: dict`.
- `require_user(request)` → read `Authorization: Bearer <t>`, verify via `request.app.state.container.token_verifier.verify(t)`, build `Principal`, stash on `request.state.principal`; on `InvalidToken`/missing → generic **401**.
- Replace the Phase 1 stub in `app/api/deps.py` (keep the name/import stable).
- **Check:** valid token → principal; invalid/expired/no-bearer → 401.

### Step 3.2 — Capability RBAC (`app/api/authz.py`)
- `effective_caps(principal)` returns **only** the `caps` claim set (deny-by-default; empty if absent).
- `require_capability(*caps)` — all required; `require_any_capability(*caps)` — at least one. Missing → generic **403** (never leaks which cap).
- Generic anti-escalation: a `role` claim alone never grants a capability (caps are the sole authority — mirrors ammar's R5-A fix, generalized).
- **Check:** no-caps token → 403; token with the cap → 200; any-cap logic correct.

### Step 3.3 — Tenant / subject context (`app/api/context.py`)
- Helpers to read `principal.tenant` / `principal.subject` from `request.state`, and a `tenant_scope(request)` dependency for downstream datastore scoping.
- **Check:** tenant claim surfaces on `request.state`; absent tenant handled safely.

### Step 3.4 — Auth rate limiting (`app/api/ratelimit.py`)
- `rate_limit(request)` dependency keys by principal-or-client-IP and calls `container.rate_limiter.allow(key)`; over limit → generic **429**. Fail-closed if the limiter errors.
- **Check:** exceeding `RATELIMIT_MAX` in the window → 429.

### Step 3.5 — Bundle CWE catalog (`app/security/cwe.json` + `catalog.py`)
- Copy `docs/cwe.json` → `app/security/cwe.json`.
- `CweCatalog`: lazy-load, index `weaknesses`/`categories`/`views`; `resolve(id) -> {id,name,type}`; `is_weakness(id)`. A Category/View can never be reported as a Weakness.
- **Check:** `resolve("CWE-89")` → name + type `weakness`; unknown id → clear error.

### Step 3.6 — Step→CWE map (`app/security/step_cwe_map.toml` + loader)
- TOML mapping each step type to the CWE ids + checks that apply, e.g.:
  ```toml
  [steps.db_query]   ; cwe = ["CWE-89"] ; checks = ["parameterized"]
  [steps.egress]     ; cwe = ["CWE-918"]; checks = ["ssrf_allowlist"]
  [steps.file_write] ; cwe = ["CWE-22"] ; checks = ["safe_object_key"]
  [steps.respond]    ; cwe = ["CWE-200","CWE-532"]; checks = ["no_secret_leak"]
  ```
- Loader uses stdlib `tomllib`; a built-in default applies if the file is absent (zero-hardcoded, overridable).
- **Check:** loader returns the map; missing file → sane default.

### Step 3.7 — CweGuard engine (`app/security/cwe_guard.py`)
- A registry `check_name -> GuardCheck` with optional `pre(ctx)` and `post(ctx, result)`.
- `guard.step(step_type, **ctx)` async context manager: on enter runs all `pre` checks for the step's mapped checks; on exit runs `post` checks against the produced result.
- A failed precondition raises `CweViolation(cwe_id, check, msg)` → **fail closed**; logged with the CWE id + name from the catalog; mapped to a safe generic error at the API boundary.
- `guard.report()` returns structured findings (for CI/audit).
- **Check:** entering a step with a violated precondition raises `CweViolation` carrying the right CWE id.

### Step 3.8 — Built-in checks (`app/security/checks.py`)
- `ssrf_allowlist` (CWE-918): `ctx["url"]` host must be in the configured egress allowlist.
- `safe_object_key` (CWE-22): `ctx["key"]` must pass `validate_key`.
- `no_secret_leak` (CWE-200/532): post-check scans the result for configured sensitive markers.
- `parameterized` (CWE-89): asserts `ctx["parameterized"] is True` (structural contract that no string-built SQL is used).
- Allowlists/markers come from **config** (zero-hardcoded).
- **Check:** each built-in blocks its bad case and passes its good case.

### Step 3.9 — Wire guard + example (`app/api/v1`)
- Expose the guard on `app.state.cwe_guard`; add a `get_guard` dependency.
- Add one guarded example route demonstrating a `guard.step(...)` around an egress/file op (removable per service).
- Optionally add `security.cwe` summary to the manifest.
- **Check:** example route uses the guard end-to-end.

### Step 3.10 — Tests (`tests/security`, `tests/integration`)
- **Auth:** valid→200/principal, invalid/expired/no-bearer→401.
- **RBAC:** no-caps→403, has-cap→200, any-cap logic, role-only never grants.
- **Rate limit:** over limit→429.
- **Catalog:** resolves weakness vs category/view; unknown id errors.
- **Guard:** pre blocks SSRF + traversal + non-parameterized; post flags secret leak; unknown step type is a safe no-op; violation carries the CWE id.
- **Check:** all green.

### Step 3.11 — Hardening & full verification
- Alg pinned (Phase 2), deny-by-default confirmed, guard fail-closed, generic errors (no leak), zero-infra boot, live boot with a real `hs256`/`rs256` token exercising a guarded route.
- **Check:** full suite green; live boot verified.

---

## 3. Out of scope (deferred)

- **CI security gates** (SAST, CVE scan, secret scan, license CI, SBOM, container scan) → Phase 4.
- **`create-service` generator** → Phase 5.
- **Domain capabilities / real IdP** — services define their own caps; identity-service (Tier 1) issues tokens later.

---

## 4. Execution checklist (tick as we build)

> **Sync rule:** each item is a task; mark it `completed` in the task list **and** tick it here.

| Task | Step | Status |
|---|---|---|
| #24 | 3.1 Principal + real auth | ✅ completed |
| #25 | 3.2 Capability RBAC | ✅ completed |
| #26 | 3.3 Tenant/subject context | ✅ completed |
| #27 | 3.4 Auth rate limiting | ✅ completed |
| #28 | 3.5 CWE catalog bundle + loader | ✅ completed |
| #29 | 3.6 Step→CWE map (toml) | ✅ completed |
| #30 | 3.7 CweGuard engine | ✅ completed |
| #31 | 3.8 Built-in checks | ✅ completed |
| #32 | 3.9 Wire guard + example | ✅ completed |
| #33 | 3.10 Tests | ✅ completed |
| #34 | 3.11 Hardening & full verification | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (106/106 tests) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this matters

- **The service defends itself** — not just "we wrote secure code," but a runtime + CI gate that names the exact weakness class (CWE) it blocks, before and after each risky step.
- **Deny-by-default everywhere** — no token, no caps, no configured verifier ⇒ no access.
- **Config-driven guard** — which CWEs apply to which step is data, so a service tunes its own risk posture without touching code.

_When every box is ✅, Phase 3 is done. Next: `docs/phases/phase-4-ci-security-gates.md`._
