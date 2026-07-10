# Phase 1 — Template Core (detailed steps)

> **Goal:** produce the minimal, hardened, self-contained FastAPI skeleton (`services/_template`)
> that every future service is cloned from. **No ports/adapters yet** (Phase 2), **no CWE guard yet**
> (Phase 3), **no CI gates yet** (Phase 4) — only the core app, config, security headers, the three
> endpoint tiers, health/metrics, safe errors, and structured logging.
>
> _Reference: `docs/architecture-plan.md` §1 (rules), §3 (layout), §6 (security tiers)._
> _Rules that apply now: self-contained · zero-hardcoded · fail-fast config · secure-by-default · permissive licenses._

---

## 0. Definition of done (Phase 1)

Phase 1 is complete when **all** are true: — ✅ **ALL MET (2026-07-08, 24/24 tests pass)**

- [x] `services/_template` boots with `uvicorn` using **zero external infra**. _(verified: live boot, healthz+metrics 200)_
- [x] Config is Pydantic `BaseSettings`, **fails fast** on invalid/missing required values, **zero hardcoded** values in code. _(verified: ValidationError on missing token)_
- [x] Security headers present on every response (CSP, HSTS, `Permissions-Policy`, no-sniff, referrer) — **including catch-all 500s**.
- [x] Three endpoint tiers exist: **public** (`/healthz` `/readyz` `/metrics`), **internal** (`/internal/*` via `X-Internal-Token`, timing-safe), **user** (`/api/v1/*` deny-by-default).
- [x] `/healthz`, `/readyz`, `/metrics`, `/internal/manifest` all respond.
- [x] Errors are **safe** (generic codes, no stack/message leak). _(verified: secret in exception never reaches client)_
- [x] Logs are **structured JSON** with secrets/auth headers **redacted** + a request-id per request.
- [x] `.env.example` documents **every** knob; `LICENSE` (Apache-2.0) and `README.md` present.
- [x] Smoke test passes: run app → probe endpoints → correct status + shapes.

---

## 1. Prerequisites & decisions (confirm before coding)

1. **Python version** — target 3.12+ (state exact in `pyproject.toml`).
2. **Package manager** — `uv` or `pip` + `venv` (pick one; document in README).
3. **License** — MIT or Apache-2.0 (Apache-2.0 recommended for patent grant).
4. **Dependencies (permissive-only)** for Phase 1: `fastapi`, `uvicorn[standard]`, `pydantic`,
   `pydantic-settings`, `structlog` (or stdlib logging), `prometheus-client`. Confirm each is MIT/BSD/Apache/ISC.
5. **Folder** — created at `services/_template/` (mirrors plan §3).

> Nothing above is hardcoded later — versions live in `pyproject.toml`, runtime values in env.

---

## 2. Steps

### Step 2.1 — Scaffold folder & project metadata
- Create `services/_template/`.
- Add `pyproject.toml`: project name placeholder `{service-name}`, Python version, pinned deps, tool config (ruff/black optional).
- Add `LICENSE`, `.gitignore`, `.dockerignore`, `README.md` (how to clone → customize).
- **Check:** `pip install -e .` (or `uv sync`) succeeds; no non-permissive license pulled in.

### Step 2.2 — Config (`app/config.py`) — fail-fast, zero-hardcoded
- `Settings(BaseSettings)` with **all** knobs as fields + safe defaults that reach **no external system**:
  - `SERVICE_NAME` (default `{service-name}`), `ENV` (`development|production|test`, default `production`),
    `PORT` (default `8000`), `LOG_LEVEL` (default `info`).
  - `INTERNAL_SERVICE_TOKEN` (**required**, min length 32) — validated.
  - `REQUEST_TIMEOUT_MS` (default `30000`), `BODY_LIMIT_BYTES` (default `1048576`).
  - `ALLOWED_FRAME_ANCESTORS`, `CORS_*` left to ingress (documented, not app-enforced by default).
- Parse at import/boot; **raise and exit** on validation error (fail-fast).
- **Check:** unset `INTERNAL_SERVICE_TOKEN` → process refuses to boot with a clear message.

### Step 2.3 — Structured logging (`app/observability/logging.py`)
- JSON logs, one line per event, ISO timestamps, `service` + `env` fields.
- **Redact**: `authorization`, `cookie`, `password`, `token`, `secret`, `*_token`.
- Silent in `test` env.
- **Check:** a log line containing a fake token shows `[REDACTED]`.

### Step 2.4 — Request middleware (`app/middleware/`)
- `request_id`: accept incoming `X-Request-Id` or generate one; attach to logs + response header.
- `security_headers`: CSP, HSTS, `Permissions-Policy`, `X-Content-Type-Options: nosniff`, referrer policy, hide server banner.
- `timeout`: abort requests over `REQUEST_TIMEOUT_MS`.
- `body_limit`: reject bodies over `BODY_LIMIT_BYTES` (413).
- **Check:** response carries all security headers + an `X-Request-Id`.

### Step 2.5 — Safe error handling (`app/api/errors.py`)
- Global exception handler: map to generic codes (`BAD_REQUEST`, `NOT_FOUND`, `INTERNAL_ERROR`).
- **Never** return `str(exception)` or a stack trace. Log full detail server-side (redacted), return generic client-side.
- 404 + 405 handlers return generic JSON.
- **Check:** force an unhandled error → client sees `{"error":"INTERNAL_ERROR"}`, server log has detail.

### Step 2.6 — Public tier (`app/api/health.py`, `app/api/metrics.py`)
- `GET /healthz` — liveness, always `200 {"status":"ok"}`.
- `GET /readyz` — readiness; Phase 1 has no deps → `200`; leave a hook for Phase 2 dependency probes.
- `GET /metrics` — Prometheus exposition (`prometheus-client`), no auth.
- **Check:** all three return expected content types + statuses.

### Step 2.7 — Internal tier (`app/api/internal.py`) — token auth
- Dependency `require_internal_token`: read `X-Internal-Token`, **timing-safe compare** vs `INTERNAL_SERVICE_TOKEN`; 401 on mismatch/missing.
- `GET /internal/health` — detailed internal status (guarded).
- `GET /internal/manifest` — return `app/manifest.py` descriptor (name, version, capabilities, endpoints; ports/events empty for now).
- **Check:** call without token → 401; with correct token → 200.

### Step 2.8 — User tier placeholder (`app/api/v1/__init__.py`)
- Empty `APIRouter` mounted at `/api/v1`, with a `TODO` comment for business routes.
- A stub auth dependency `require_user` that **denies by default** (Phase 3 wires real `TokenVerifier`).
- **Check:** `/api/v1/` mounts cleanly; stub dep returns 401 until Phase 3.

### Step 2.9 — Manifest (`app/manifest.py`)
- Static descriptor built from config: `name`, `version`, `capabilities: []`, `endpoints: [...]`, `ports: []`, `events: {emitted:[],consumed:[]}`.
- **Check:** `/internal/manifest` returns valid JSON matching the schema.

### Step 2.10 — App factory & lifespan (`app/main.py`)
- `create_app()`: build FastAPI, register middleware (order: request-id → security-headers → timeout → body-limit → logging), mount routers (public → internal → v1), attach error handlers.
- `lifespan`: startup log + graceful shutdown hook (no resources yet).
- Entry: `uvicorn app.main:app`.
- **Check:** `uvicorn` starts, logs a startup line, serves all tiers.

### Step 2.11 — `.env.example` + README
- `.env.example`: every knob from `config.py`, documented, **no real values** (placeholders only).
- README: what the template is, clone/customize steps, run command, endpoint table, "Phase 1 scope" note.
- **Check:** copying `.env.example` → `.env` and filling `INTERNAL_SERVICE_TOKEN` lets the app boot.

### Step 2.12 — Smoke test (`tests/integration/test_smoke.py`)
- Boot app in-process (TestClient); assert:
  - `/healthz` 200, `/readyz` 200, `/metrics` 200 text.
  - `/internal/manifest` 401 without token, 200 with token.
  - security headers + `X-Request-Id` present.
  - forced error → generic `INTERNAL_ERROR`, no leak.
- **Check:** `pytest` green.

---

## 3. Out of scope (deferred — do NOT build in Phase 1)

- **Ports & adapters / `container.py`** → Phase 2.
- **`TokenVerifier`, real authz, `SecretsProvider`, rate limiting, `CweGuard` + `cwe.json`** → Phase 3.
- **CI gates (SAST, CVE scan, secret scan, license CI, SBOM, container scan)** → Phase 4.
- **`create-service` scaffolder + first real service** → Phase 5.

---

## 4. Execution checklist (tick as we build)

> **Sync rule:** each item is a task in the task list. When a task is completed, mark it `completed`
> in the task list **and** tick its box here. The two must always match.

| Task | Step | Status |
|---|---|---|
| #1  | 2.1 Scaffold & metadata | ✅ completed |
| #2  | 2.2 Config (fail-fast) | ✅ completed |
| #3  | 2.3 Logging (redacted JSON) | ✅ completed |
| #4  | 2.4 Middleware (request-id, headers, timeout, body-limit) | ✅ completed |
| #5  | 2.5 Safe errors | ✅ completed |
| #6  | 2.6 Public tier (health/ready/metrics) | ✅ completed |
| #7  | 2.7 Internal tier (token + manifest) | ✅ completed |
| #8  | 2.8 User tier placeholder (deny-by-default) | ✅ completed |
| #9  | 2.9 Manifest | ✅ completed |
| #10 | 2.10 App factory + lifespan | ✅ completed |
| #11 | 2.11 `.env.example` + README | ✅ completed |
| #12 | 2.12 Smoke test green | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (24/24 tests) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

_When every box is ✅, Phase 1 is done. Next: `docs/phases/phase-2-ports-adapters.md`._
