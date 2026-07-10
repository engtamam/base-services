# Phase 5 — Smart Service Generator (detailed steps)

> **Goal:** the scaffolder that **beats ammar's** — a spec-driven, safe, and
> **self-verifying** generator. It clones the `_template` into a new
> self-contained service, picks the ports/adapters you want, then **generates →
> installs/reuses env → runs the tests + the security gate → reports green/red**.
> If verification fails, it **rolls back**.
>
> _Reference: `docs/architecture-plan.md` §10 (scaffolder); the ammar comparison in chat._
> _Rules: self-contained output · zero-hardcoded · production-ready · no-silent-gaps · atomic/rollback._

---

## 0. Definition of done (Phase 5)

- [ ] `scripts/create_service.py <name>` clones `services/_template` → `services/<name>`, a **100% self-contained** service.
- [ ] **Strict, safe input** — kebab name validated; **no path traversal**; refuses to overwrite unless `--force`.
- [ ] **Adapter picking** — flags (or a `--spec` TOML) choose the driver per port; the choices are written into the new service's `.env` + a recorded `service.spec.toml`.
- [ ] **Placeholder engine** — replaces `{service-name}` / `service-template` everywhere safely (skips binaries + the bundled `cwe.json`); the package dir stays `app/`.
- [ ] **Atomic + rollback** — builds in a temp dir, verifies, then moves into place; **any failure cleans up** and leaves the repo untouched.
- [ ] **Self-verifying** — runs `pytest` + `ci/security_gate.py` against the generated service (reusing the template venv when available, else creating one); **fails red** if either fails.
- [ ] **`--dry-run`** prints the plan (name, port, drivers, file count) and writes nothing.
- [ ] **Port auto-assign** — a lightweight registry avoids collisions; `--port` overrides.
- [ ] **Clear report** — files created, port, drivers, verification result, next steps.
- [ ] **Tests** for the generator itself pass; an **end-to-end generation** produces a service whose own tests + gate are green.

---

## 1. Prerequisites & decisions

1. **Location.** The generator is **repo tooling** (`base-services/scripts/create_service.py`), not part of a service — so the self-contained rule (services own no shared code) is preserved. Generated services go to `services/<name>`.
2. **Package stays `app/`.** Every service uses the same `app/` package; only metadata changes (`pyproject` name, `SERVICE_NAME`, headers). This keeps replacement tiny and safe.
3. **Fast verify via venv reuse.** Runtime deps are identical across services, so verification reuses the template's `.venv` python by default (`--python`), avoiding a multi-minute reinstall; falls back to creating a fresh venv when none is given.
4. **Spec-driven & reproducible.** Choices live in `service.spec.toml` (stdlib `tomllib` read, hand-written TOML write) — same spec → same service.
5. **Stdlib only.** The generator uses only the standard library (argparse, shutil, subprocess, tomllib, secrets, re) — no new dependency.

---

## 2. Steps

### Step 5.1 — CLI + strict validation (`scripts/create_service.py`)
- Args: `name` (positional), `--port`, `--desc`, per-port driver flags (`--datastore`, `--cache`, `--event-bus`, `--queue`, `--object-store`, `--secrets`, `--token-verifier`, `--rate-limiter`), `--spec FILE`, `--python PATH`, `--dry-run`, `--force`, `--no-verify`.
- Validate `name` against `^[a-z][a-z0-9-]{1,48}[a-z0-9]$`; reject anything with `/`, `\`, `..`, path separators (traversal, CWE-22).
- Validate each driver against the known allowlist (matches the container registries).
- **Check:** bad name / unknown driver / existing dir (no `--force`) → clear error, exit non-zero.

### Step 5.2 — Spec + port registry
- `service.spec.toml` schema: `name`, `description`, `port`, `[drivers]`. Load via `tomllib`; CLI flags override spec.
- Port registry `services/.registry.json` (`name -> port`); auto-assign the next free from a base (default 8001), skipping used; `--port` overrides + is recorded.
- **Check:** two generations get distinct ports; registry updates.

### Step 5.3 — Copy + placeholder engine (atomic temp build)
- Copy `services/_template` → a temp dir, **excluding** `.venv`, `.git`, `__pycache__`, `.pytest_cache`, `.objectstore`, `sbom.cdx.json`.
- Replace tokens `{service-name}` and `service-template` → `<name>` in text files; **skip** binaries and `app/security/cwe.json` (huge, tokenless); catch decode errors.
- **Check:** placeholders gone; `app/` unchanged; `cwe.json` untouched byte-for-byte.

### Step 5.4 — Adapter/driver selection
- Write a generated `.env` in the new service: chosen `*_DRIVER` values + a fresh random `INTERNAL_SERVICE_TOKEN` (so it boots) + `SERVICE_NAME=<name>` + `PORT`.
- Set `pyproject` `name`, `SERVICE_NAME` default, Dockerfile/Makefile tags to `<name>`.
- Write `service.spec.toml` (the record of choices).
- **Check:** `.env` + spec reflect selections; service boots with them.

### Step 5.5 — Self-verify + rollback
- In the temp build, run `pytest -q` and `ci/security_gate.py` using `--python` (default: template `.venv`), else create a venv + `pip install -e ".[dev,ci]"`.
- If either fails (and not `--no-verify`): **abort**, delete temp, exit red — repo untouched.
- **Check:** a deliberately broken spec fails red and leaves nothing behind.

### Step 5.6 — Commit (atomic move) + report + dry-run
- On success: move temp → `services/<name>`; update the port registry.
- Print a summary: path, port, drivers, files created, verification result, next steps.
- `--dry-run`: print the plan only; write nothing (no temp kept).
- **Check:** dry-run writes nothing; real run produces a verified service + report.

### Step 5.7 — Tests (`scripts/tests/` or `tests/generator/`)
- Unit: name validation (good/bad/traversal), driver allowlist, placeholder replacement, spec load + flag override, port auto-assign, dry-run writes nothing.
- **Check:** green.

### Step 5.8 — End-to-end + docs
- Generate a sample service (e.g. `sample-svc`) with a couple of non-default drivers, **verify it green** (its own pytest + gate), then remove it (or keep as an example).
- Write `scripts/README.md` (usage) and update `docs/architecture-plan.md` build-order note.
- **Check:** e2e service is green; docs explain usage.

---

## 3. Out of scope (deferred)

- **Interactive wizard TUI** — flags + `--spec` cover it; a wizard can be added later.
- **Tier-1 services themselves** (identity, tenant, …) — Phase 6+, generated *with* this tool.
- **Monorepo CI matrix** across many services — later.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #46 | 5.1 CLI + strict validation | ✅ completed |
| #47 | 5.2 Spec + port registry | ✅ completed |
| #48 | 5.3 Copy + placeholder engine | ✅ completed |
| #49 | 5.4 Adapter/driver selection | ✅ completed |
| #50 | 5.5 Self-verify + rollback | ✅ completed |
| #51 | 5.6 Commit + report + dry-run | ✅ completed |
| #52 | 5.7 Tests | ✅ completed (24/24) |
| #53 | 5.8 End-to-end + docs | ✅ completed (sample-svc verified green) |
| —   | DoD (§0) all checked | ✅ completed |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why it beats ammar

| ammar `create-service.mjs` | our `create_service.py` |
|---|---|
| blind string replace, no verify | placeholder engine + **self-verify (tests + security gate)** |
| hardcoded contexts/port scheme | config/spec-driven, generic |
| no adapter choices | **picks ports/adapters**, writes them in |
| partial mess on failure | **atomic temp → move, rollback** |
| no record of choices | **`service.spec.toml`** (reproducible) |
| no dry-run | **`--dry-run`** plan |

_When every box is ✅, Phase 5 is done — and the platform can mint new, verified, production-ready services on demand._
