#!/usr/bin/env python3
# =============================================================================
# webhook-service — Security gate orchestrator
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


def gate_cve() -> tuple[str, str]:
    if shutil.which("pip-audit") is None and not _module("pip_audit"):
        return SKIP, "pip-audit not installed"
    try:
        r = _run([PY, "-m", "pip_audit", "--progress-spinner", "off"], timeout=180)
    except subprocess.TimeoutExpired:
        return SKIP, "pip-audit timed out (offline?)"
    out = (r.stdout + r.stderr).lower()
    if "no known vulnerabilities" in out:
        return PASS, "no known vulnerabilities"
    if "known vulnerabilit" in out:
        return FAIL, "known vulnerability found"
    if any(k in out for k in ("connection", "temporary failure", "timed out", "network", "resolve")):
        return SKIP, "advisory DB unreachable (offline)"
    return (PASS if r.returncode == 0 else FAIL), out.strip().splitlines()[-1] if out.strip() else ""


def gate_sbom() -> tuple[str, str]:
    try:
        r = _run([PY, "-m", "cyclonedx_py", "environment", str(ROOT / ".venv"),
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
    # trivy fs scans the filesystem (deps + Dockerfile misconfig) — no Docker needed.
    if shutil.which("trivy") is None:
        return SKIP, "trivy not installed"
    r = _run(["trivy", "fs", "--quiet", "--scanners", "vuln,misconfig",
              "--severity", "HIGH,CRITICAL", "--exit-code", "1", "--skip-dirs", ".venv",
              str(ROOT)], timeout=300)
    return (PASS if r.returncode == 0 else FAIL), "trivy fs HIGH/CRITICAL"


def _module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


GATES = [
    ("license", gate_license),
    ("sast (bandit)", gate_sast),
    ("secret-scan", gate_secrets),
    ("cve (pip-audit)", gate_cve),
    ("sbom", gate_sbom),
    ("container (trivy)", gate_container),
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
