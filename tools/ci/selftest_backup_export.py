#!/usr/bin/env python
"""Prove safety:backup-export can FAIL: a service that neither serves export nor is exempt fails; a
served one and a reasoned exemption pass."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_backup_export.py"

SERVES = ('ENDPOINTS = [{"path": "/internal/backup/export", "tier": "internal", "method": "POST"}]\n')
NO_EXPORT = 'ENDPOINTS = [{"path": "/internal/health", "tier": "internal", "method": "GET"}]\n'


def _repo(tmp: pathlib.Path, manifest: str) -> None:
    app = tmp / "services" / "demo-service" / "app"
    app.mkdir(parents=True)
    (app / "manifest.py").write_text(manifest, encoding="utf-8")
    (tmp / "services" / "_template" / "app").mkdir(parents=True)


def _run(tmp: pathlib.Path, exempt: str = "") -> int:
    env = dict(os.environ, BACKUP_EXPORT_GATE_ROOT=str(tmp))
    env.pop("BACKUP_EXPORT_GATE_EXCLUDE", None)
    if exempt:
        env["BACKUP_EXPORT_GATE_EXCLUDE"] = exempt
    return subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, env=env).returncode


def main() -> int:
    fails = []
    cases = [
        ("serves export -> pass", SERVES, "", 0),
        ("no export, not exempt -> fail", NO_EXPORT, "", 1),
        ("no export, exempt -> pass", NO_EXPORT, "demo-service", 0),
    ]
    for name, manifest, exempt, want in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            _repo(tmp, manifest)
            rc = _run(tmp, exempt)
        if rc != want:
            fails.append(f"  {name}: expected exit {want}, got {rc}")
    if fails:
        print("backup-export selftest FAILED:\n" + "\n".join(fails))
        return 1
    print(f"      backup-export selftest OK — {len(cases)} cases as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
