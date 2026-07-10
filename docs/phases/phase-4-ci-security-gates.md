# Phase 4 — CI Security Gates (detailed steps)

> **Goal:** turn our security rules into an **automatic gate**. One command (local +
> CI) runs every check and **blocks the build** on any breach — no human needed.
> Gates: **license** (permissive-only), **SAST**, **secret scan**, **dependency
> CVE scan**, **SBOM**, **container scan** — plus a hardened Dockerfile.
>
> _Reference: `docs/architecture-plan.md` §1 rule 6 (licenses) + rule 8 (standards), §9 (CI security gate)._
> _Rules that apply: self-contained · permissive-licenses-only · standards-first · no-silent-gaps._

---

## 0. Definition of done (Phase 4)

- [ ] **License gate** fails the build on any non-permissive dependency (allow MIT/BSD/Apache-2.0/ISC/PSF; forbid GPL/LGPL/AGPL/MPL/SSPL/BSL/…); passes on our deps.
- [ ] **SAST gate** (Bandit) runs on `app/`; no Medium/High findings (or explicit, justified `# nosec`).
- [ ] **Secret-scan gate** (detect-secrets) with a clean, audited baseline; new secrets fail the build.
- [ ] **Dependency CVE gate** (pip-audit) runs; a known vulnerability fails the build.
- [ ] **SBOM** (CycloneDX JSON) is generated for the environment.
- [ ] **Hardened Dockerfile** — slim, **non-root**, read-only-friendly; **container scan** (Trivy) runs when available.
- [ ] **One orchestrator** (`ci/security_gate.py`) runs all gates, prints a summary, exits non-zero on any failure. **No silent skips** — an unrunnable gate is reported loudly; `--strict` turns SKIP into FAIL (CI uses it).
- [ ] **GitHub Actions** workflow runs the gate on push/PR.
- [ ] All **offline gates pass green** on our current code + deps.

---

## 1. Prerequisites & decisions

1. **CI tools as a `[ci]` extra — all permissive:** `bandit` (Apache-2.0), `pip-audit` (Apache-2.0), `detect-secrets` (Apache-2.0), `cyclonedx-bom` (Apache-2.0). Trivy is an external binary (Apache-2.0), used only if present.
2. **Online vs offline.** License, SAST, secret-scan, SBOM run **offline**. CVE scan needs the advisory DB (network); container scan needs Trivy+image. The orchestrator marks an unrunnable online gate **SKIP (reason)** locally; **`--strict`** (CI) makes SKIP a failure — so CI has no gaps.
3. **License checker is our own** (`ci/check_licenses.py`, stdlib `importlib.metadata`) — deterministic, with a curated override map for packages with messy metadata; an **unknown** license fails (forces vetting every new dep).
4. **Dockerfile** added now (needed for container scan + deploy): multi-stage, `python:3.12-slim`, non-root user, runtime deps only.

---

## 2. Steps

### Step 4.1 — CI tool deps + configs
- Add `[project.optional-dependencies].ci = [bandit, pip-audit, detect-secrets, cyclonedx-bom]`.
- `[tool.bandit]` config in `pyproject.toml` (targets `app/`, sensible skips with justification).
- **Check:** `pip install -e ".[ci]"` succeeds; all tools importable.

### Step 4.2 — License gate (`ci/check_licenses.py`)
- Enumerate installed distributions; classify each license (classifiers + `License` field + override map).
- **Allow:** MIT, BSD-2/3, Apache-2.0, ISC, PSF/Python-2.0, 0BSD/ISC, HPND. **Forbid:** GPL, LGPL, AGPL, MPL, SSPL, BUSL/BSL, EUPL, CDDL, EPL, Commons-Clause, "non-commercial". **Unknown → fail.**
- Print a table; exit non-zero on any violation/unknown.
- **Check:** passes on our deps; a synthetic forbidden entry fails (unit test).

### Step 4.3 — SAST gate (Bandit)
- Run `bandit -r app` at Medium severity/confidence; fix or `# nosec <ID>` (justified) any finding.
- **Check:** 0 unresolved Medium/High findings.

### Step 4.4 — Secret-scan gate (detect-secrets)
- Generate `.secrets.baseline`, audit it (must contain no real secrets — only test placeholders).
- Gate: scan and diff against baseline; new detections fail.
- **Check:** clean scan; a planted fake secret is detected (manual/unit).

### Step 4.5 — Dependency CVE gate (pip-audit)
- Run `pip-audit` against the environment; a known CVE fails. Document any accepted/ignored advisory with reason.
- **Check:** runs; green (or documented ignores).

### Step 4.6 — SBOM (CycloneDX)
- Generate `sbom.cdx.json` for the environment (`cyclonedx-py environment`), with a minimal fallback if the tool is absent.
- **Check:** valid CycloneDX JSON with our components.

### Step 4.7 — Hardened Dockerfile + container scan
- Multi-stage `Dockerfile`: builder installs runtime deps; final `python:3.12-slim`, **non-root** `USER`, only `app/` + deps, `EXPOSE`, `HEALTHCHECK`, read-only-friendly.
- Container scan: `trivy image` when Trivy + a built image are available; else SKIP (reason).
- **Check:** image builds; scan runs when possible.

### Step 4.8 — Orchestrator (`ci/security_gate.py`) + Makefile
- Runs every gate, collects PASS/FAIL/SKIP, prints a summary table, exits non-zero on any FAIL. `--strict` makes SKIP a FAIL. `make security` / `make check` wrappers.
- **Check:** one command runs all; correct exit code.

### Step 4.9 — GitHub Actions (`.github/workflows/security.yml`)
- On push/PR: set up Python 3.12, `pip install -e ".[dev,ci]"`, run `pytest`, then `python ci/security_gate.py --strict`.
- **Check:** workflow file valid; steps ordered.

### Step 4.10 — Tests
- Unit-test the license checker (allowed pass; forbidden + unknown fail).
- Smoke-test the orchestrator (imports, runs, returns a status object).
- **Check:** green.

### Step 4.11 — Full verification
- Run `python ci/security_gate.py` for real: license ✅, SAST ✅, secret-scan ✅, SBOM ✅; CVE/container run or SKIP-with-reason. Full `pytest` still green. Update README (how to run the gate).
- **Check:** offline gates green; summary correct.

---

## 3. Out of scope (deferred)

- **`create-service` generator** → Phase 5 (it will copy these gates into every service and wire CI).
- **Signing/provenance (SLSA level 3), image registry push** → later hardening.

---

## 4. Execution checklist

> **Sync rule:** each item is a task; mark `completed` in the list **and** tick here.

| Task | Step | Status |
|---|---|---|
| #35 | 4.1 CI tool deps + configs | ✅ completed |
| #36 | 4.2 License gate | ✅ completed |
| #37 | 4.3 SAST gate (Bandit) | ✅ completed |
| #38 | 4.4 Secret-scan gate | ✅ completed |
| #39 | 4.5 Dependency CVE gate | ✅ completed |
| #40 | 4.6 SBOM | ✅ completed |
| #41 | 4.7 Dockerfile + container scan | ✅ completed |
| #42 | 4.8 Orchestrator + Makefile | ✅ completed |
| #43 | 4.9 GitHub Actions | ✅ completed |
| #44 | 4.10 Tests | ✅ completed |
| #45 | 4.11 Full verification | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (127/127 tests; gate PASS) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this matters

- **Rules become enforcement** — "permissive licenses only", "no CVEs", "no secrets" stop being promises and become a gate that blocks the merge.
- **No silent gaps** — every gate reports PASS/FAIL/SKIP; CI runs `--strict` so nothing is quietly skipped.
- **Auditable** — SBOM + gate logs prove what shipped and that it was checked (NIST SSDF / SLSA).

_When every box is ✅, Phase 4 is done. Next: `docs/phases/phase-5-service-generator.md` (the smart scaffolder)._
