#!/usr/bin/env python3
# =============================================================================
# compliance-service — Security gate orchestrator
# -----------------------------------------------------------------------------
# Runs every security gate, prints a summary, exits non-zero on any FAIL.
# NO SILENT GAPS: a gate that cannot run (missing tool / offline) is reported
# loudly as SKIP with a reason. `--strict` (used in CI) turns SKIP into FAIL.
#
#   python ci/security_gate.py            # local: offline gates enforced
#   python ci/security_gate.py --strict   # CI: nothing may be skipped
# =============================================================================
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

_EXCLUDES = [
    "--exclude-files", r"\.venv/",
    "--exclude-files", r"app/security/cwe\.json",
    "--exclude-files", r"\.objectstore/",
    "--exclude-files", r"sbom\.cdx\.json",
]


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout
    )


def gate_license() -> tuple[str, str]:
    r = _run([PY, str(ROOT / "ci" / "check_licenses.py")])
    return (PASS if r.returncode == 0 else FAIL), r.stdout.strip().splitlines()[-1] if r.stdout else ""


def gate_sast() -> tuple[str, str]:
    r = _run([PY, "-m", "bandit", "-c", "pyproject.toml", "-r", "app", "-ll", "-ii", "-q"])
    # A MISSING scanner is not a finding. Reporting "bandit medium+ findings" when bandit was never
    # installed asserts a security result that was never measured — the same lie in the opposite
    # direction from a scanner that silently passes, and worse than either because it sends someone
    # hunting for vulnerabilities that were never reported. Every other scanner here already
    # distinguishes the two; this one did not. SKIP is honest, and --strict (what CI runs) still
    # turns it into a failure, so nothing gets weaker where it counts.
    if r.returncode != 0 and "No module named bandit" in (r.stderr or ""):
        return SKIP, "bandit not installed"
    return (PASS if r.returncode == 0 else FAIL), "bandit medium+ findings" if r.returncode else "no medium/high findings"


def _secret_set(stdout: str) -> set[tuple[str, str]]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return set()
    out: set[tuple[str, str]] = set()
    for fname, items in data.get("results", {}).items():
        for it in items:
            out.add((fname, it.get("hashed_secret", "")))
    return out


def gate_secrets() -> tuple[str, str]:
    r = _run([PY, "-m", "detect_secrets", "scan", *_EXCLUDES])
    if r.returncode != 0:
        return SKIP, "detect-secrets could not run"
    current = _secret_set(r.stdout)
    baseline_path = ROOT / ".secrets.baseline"
    baseline = _secret_set(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else set()
    new = current - baseline
    if new:
        return FAIL, f"{len(new)} new secret(s) detected"
    return PASS, f"{len(current)} known, 0 new"


# A remote advisory DB can fail in ways that say nothing about this code. Distinguish those from
# a real finding, or the gate becomes noise and someone starts re-running it until it is green.
#
# `jsondecodeerror` earned its place here: once the gate script began running the 18 services
# CONCURRENTLY, 18 simultaneous pip-audit calls made PyPI hand one of them a malformed body.
# Serially it never happened. Parallelism did not find a bug in the code; it found a bug in what
# we were willing to call a vulnerability.
_TRANSIENT = (
    "connection", "temporary failure", "timed out", "network", "resolve",
    "jsondecodeerror", "expecting value", "502", "503", "504", "rate limit",
    "too many requests", "remote end closed",
)
_CVE_ATTEMPTS = 3


def gate_cve() -> tuple[str, str]:
    if shutil.which("pip-audit") is None and not _module("pip_audit"):
        return SKIP, "pip-audit not installed"

    out = ""
    for attempt in range(_CVE_ATTEMPTS):
        try:
            r = _run([PY, "-m", "pip_audit", "--progress-spinner", "off"], timeout=180)
        except subprocess.TimeoutExpired:
            return SKIP, "pip-audit timed out (offline?)"
        out = (r.stdout + r.stderr).lower()

        # A real answer, either way. Never retried: a vulnerability does not go away.
        if "no known vulnerabilities" in out:
            return PASS, "no known vulnerabilities"
        if "known vulnerabilit" in out:
            return FAIL, "known vulnerability found"

        if any(k in out for k in _TRANSIENT):
            if attempt < _CVE_ATTEMPTS - 1:
                time.sleep(2 ** attempt)          # 1s, 2s — enough for a rate limiter to forget us
                continue
            return SKIP, "advisory DB unreachable (offline)"
        break

    return (PASS if r.returncode == 0 else FAIL), out.strip().splitlines()[-1] if out.strip() else ""


def gate_sbom() -> tuple[str, str]:
    # The ACTIVE interpreter's environment, not a hardcoded `.venv` path. Locally that is the
    # service's venv; in CI the service is pip-installed into the runner's python and no .venv
    # exists at all — pointing at one made this gate fail in CI and only in CI.
    try:
        r = _run([PY, "-m", "cyclonedx_py", "environment", sys.prefix,
                  "--output-format", "JSON", "-o", "sbom.cdx.json"], timeout=180)
    except subprocess.TimeoutExpired:
        return SKIP, "cyclonedx timed out"
    sbom = ROOT / "sbom.cdx.json"
    if r.returncode == 0 and sbom.exists():
        try:
            data = json.loads(sbom.read_text(encoding="utf-8"))
            return PASS, f"CycloneDX {data.get('specVersion')}, {len(data.get('components', []))} components"
        except json.JSONDecodeError:
            return FAIL, "invalid SBOM json"
    return SKIP, "cyclonedx-bom not installed"


def gate_container() -> tuple[str, str]:
    """Dockerfile MISCONFIGURATION only. Dependency CVEs are `cve (pip-audit)`'s job.

    This used to pass `--scanners vuln,misconfig` and its comment claimed it covered
    "deps + Dockerfile misconfig". It never scanned a single dependency. Trivy's pip scanner
    reads `requirements.txt` / `poetry.lock` / `uv.lock`; we ship `pyproject.toml` with no
    lockfile, so trivy resolved zero packages and reported clean. Verified both ways: a canary
    tree pinning `jinja2==2.10` + `requests==2.19.1` yields two HIGH CVEs and exit 1, while
    every service here yields `targets scanned: NONE`.

    A scan that finds nothing and a scan that scans nothing print the same green line. Naming
    the scanner honestly is the fix — `pip-audit` already audits the installed environment,
    which is what actually ships in the image.
    """
    if shutil.which("trivy") is None:
        return SKIP, "trivy not installed"

    r = _run(["trivy", "fs", "--quiet", "--scanners", "misconfig",
              "--severity", "HIGH,CRITICAL", "--exit-code", "1", "--skip-dirs", ".venv",
              str(ROOT)], timeout=300)
    if r.returncode == 0:
        return PASS, "trivy fs misconfig HIGH/CRITICAL"

    # `FAIL` with no evidence is an accusation, not a finding. trivy exits non-zero both when it
    # finds a misconfiguration and when it cannot run at all -- a cold checks-bundle, a corrupted
    # cache, a rate-limited download. Those are different facts and the operator has to be able
    # to tell them apart without re-running the gate by hand.
    detail = (r.stdout + r.stderr).strip()
    transient = any(k in detail.lower() for k in (
        "failed to download", "unable to initialize", "connection", "timeout", "rate limit",
        "too many requests", "database", "bundle", "context deadline exceeded"))
    if transient:
        return SKIP, f"trivy could not run (not a finding): {detail.splitlines()[-1][:80]}"

    first = next((ln for ln in detail.splitlines() if ln.strip()), "")
    return FAIL, f"trivy fs misconfig HIGH/CRITICAL — {first[:90]}"


def _module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


GATES = [
    ("license", gate_license),
    ("sast (bandit)", gate_sast),
    ("secret-scan", gate_secrets),
    ("cve (pip-audit)", gate_cve),
    ("sbom", gate_sbom),
    ("container (trivy misconfig)", gate_container),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run all security gates.")
    ap.add_argument("--strict", action="store_true", help="treat SKIP as FAIL (CI)")
    args = ap.parse_args()

    print("=" * 60)
    print("  Security gate")
    print("=" * 60)
    results: list[tuple[str, str, str]] = []
    for name, fn in GATES:
        try:
            status, detail = fn()
        except Exception as exc:  # a gate crash must not be a silent pass
            status, detail = FAIL, f"gate error: {type(exc).__name__}"
        results.append((name, status, detail))
        icon = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        print(f"  [{icon}] {name:20} {detail}")

    print("=" * 60)
    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    if args.strict:
        failed += skipped
    if skipped and not args.strict:
        print(f"  {len(skipped)} gate(s) SKIPPED (run with --strict in CI to enforce)")
    if failed:
        print(f"  RESULT: FAIL ({len(failed)} gate(s))")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
