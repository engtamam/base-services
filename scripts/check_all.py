#!/usr/bin/env python3
# =============================================================================
# base-services — run tests + security gate for EVERY service (monorepo, local)
# -----------------------------------------------------------------------------
# Mirrors the monorepo CI locally. Reuses the _template .venv by default (deps
# are identical across services), so it's fast. Exits non-zero if any service
# fails. Stdlib only.
#
#   python scripts/check_all.py
#   python scripts/check_all.py --python /path/to/python --strict
# =============================================================================
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "services"


def _template_python() -> str | None:
    for c in (SERVICES / "_template" / ".venv" / "Scripts" / "python.exe",
              SERVICES / "_template" / ".venv" / "bin" / "python"):
        if c.exists():
            return str(c)
    return None


def discover() -> list[Path]:
    return sorted(p.parent for p in SERVICES.glob("*/pyproject.toml"))


def check(service: Path, py: str, strict: bool) -> tuple[str, bool, str]:
    t = subprocess.run([py, "-m", "pytest", "-q"], cwd=service, capture_output=True, text=True)
    if t.returncode != 0:
        return service.name, False, "pytest failed"
    cmd = [py, "ci/security_gate.py"] + (["--strict"] if strict else [])
    g = subprocess.run(cmd, cwd=service, capture_output=True, text=True)
    if g.returncode != 0:
        return service.name, False, "security gate failed"
    return service.name, True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run tests + gate for every service.")
    ap.add_argument("--python", default=None, help="interpreter (default: _template .venv)")
    ap.add_argument("--strict", action="store_true", help="strict gate (SKIP -> FAIL)")
    args = ap.parse_args()

    py = args.python or _template_python() or sys.executable
    services = discover()
    if not services:
        print("no services found under services/*")
        return 0

    print(f"Checking {len(services)} service(s) with {py}\n")
    results = [check(s, py, args.strict) for s in services]
    for name, ok, detail in results:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:24} {detail}")

    failed = [r for r in results if not r[1]]
    print("\nRESULT:", "PASS" if not failed else f"FAIL ({len(failed)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
