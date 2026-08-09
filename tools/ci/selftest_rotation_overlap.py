#!/usr/bin/env python
"""Prove safety:rotation-overlap FAILS on a hard-cutover rotate, PASSES with a timestamp overlap or
an explicit exclusion."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_rotation_overlap.py"

HARD = "class X:\n    async def rotate(self, a):\n        a.secret_hash = h(new())\n        return s\n"
GOOD = ("class X:\n    prev_secret_expires = 0.0\n"
        "    async def rotate(self, a, overlap_seconds=0):\n"
        "        a.prev_secret_hash = a.secret_hash; a.prev_secret_expires = now()+overlap_seconds\n"
        "        a.secret_hash = h(new()); return s\n")


def _repo(tmp: pathlib.Path, body: str) -> None:
    core = tmp / "services" / "demo-service" / "app" / "core"
    core.mkdir(parents=True)
    (core / "rot.py").write_text(body, encoding="utf-8")


def _run(tmp: pathlib.Path, exclude: str = "") -> int:
    env = dict(os.environ, ROTATION_GATE_ROOT=str(tmp))
    env.pop("ROTATION_GATE_EXCLUDE", None)
    if exclude:
        env["ROTATION_GATE_EXCLUDE"] = exclude
    return subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, env=env).returncode


def main() -> int:
    fails = []
    cases = [
        ("timestamp overlap -> pass", GOOD, "", 0),
        ("hard cutover -> fail", HARD, "", 1),
        ("hard cutover but excluded -> pass", HARD, "demo-service:rotate", 0),
    ]
    for name, body, exclude, want in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            _repo(tmp, body)
            rc = _run(tmp, exclude)
        if rc != want:
            fails.append(f"  {name}: expected {want}, got {rc}")
    if fails:
        print("rotation-overlap selftest FAILED:\n" + "\n".join(fails))
        return 1
    print(f"      rotation-overlap selftest OK — {len(cases)} cases as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
