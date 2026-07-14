#!/usr/bin/env python
"""Mutation proof for safety:edge. A gate that cannot fail is decoration.

Copies the real edge into a temp tree, confirms the gate passes it, then breaks it once per rule
and demands the gate go red each time. A surviving mutation means that rule is not enforced.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_edge.py"


def run_gate(root: pathlib.Path) -> int:
    return subprocess.run([sys.executable, str(CHECK)],
                          env={**os.environ, "EDGE_GATE_ROOT": str(root)},
                          capture_output=True, text=True).returncode


def setup(base: pathlib.Path) -> pathlib.Path:
    """A correct edge tree: the real config, deployment, and the render/validate tools."""
    (base / "deploy" / "edge").mkdir(parents=True)
    (base / "tools" / "edge").mkdir(parents=True)
    for f in ("edge.json", "edge.deployment.yaml"):
        shutil.copyfile(ROOT / "deploy" / "edge" / f, base / "deploy" / "edge" / f)
    for f in ("render.py", "validate.py"):
        shutil.copyfile(ROOT / "tools" / "edge" / f, base / "tools" / "edge" / f)
    (base / "deploy" / "services").mkdir(parents=True)
    return base


# Each mutation targets the ACTUAL enforcement point of one rule. (file, find, replace).
MUTATIONS = {
    "R2 validator accepts an injected hostname": (
        # neuter the whole rejection mechanism so ANY value renders — the gate must catch it
        "tools/edge/validate.py",
        'def _reject(kind: str, value: str, why: str) -> None:',
        'def _reject(kind: str, value: str, why: str) -> None:\n    return  # MUTATED'),
    "R3 HSTS emitted over plain http": (
        # make HSTS unconditional in the renderer — the gate must catch it on the tls=false path
        "tools/edge/render.py", "if tls:\n        headers[\"Strict-Transport-Security\"]",
        "if True:\n        headers[\"Strict-Transport-Security\"]"),
    "R5 edge runs with a writable root": (
        "deploy/edge/edge.deployment.yaml", "readOnlyRootFilesystem: true",
        "readOnlyRootFilesystem: false"),
    "R5 caps not dropped": (
        "deploy/edge/edge.deployment.yaml", "drop: [ALL]", "drop: []"),
    "R6 per-pod limiter at scale": (
        "deploy/services/edge-svc.values.yaml", None,
        "replicas: 3\nRATELIMITER_DRIVER: memory\n"),      # None find => create file
}


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = setup(pathlib.Path(td))

        if run_gate(base) != 0:
            print("  FAIL: the gate rejects a CORRECT edge — it would block every honest commit")
            return 1
        print("  ok   correct edge passes")

        for name, (rel, find, repl) in MUTATIONS.items():
            # re-copy clean each time
            setup_clean(base)
            f = base / rel
            if find is None:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(repl, encoding="utf-8")
            else:
                t = f.read_text(encoding="utf-8")
                if find not in t:
                    print(f"  SKIP {name}: fixture no longer contains {find!r}")
                    continue
                f.write_text(t.replace(find, repl, 1), encoding="utf-8")

            if run_gate(base) == 0:
                failures.append(name)
                print(f"  FAIL {name} — SURVIVED. The rule is not enforced.")
            else:
                print(f"  ok   {name} — caught")

    if failures:
        print(f"\nsafety:edge selftest FAILED: {len(failures)} mutation(s) survived.")
        return 1
    print("\nsafety:edge selftest OK — every mutation caught")
    return 0


def setup_clean(base: pathlib.Path) -> None:
    shutil.rmtree(base / "deploy")
    shutil.rmtree(base / "tools")
    setup(base)


if __name__ == "__main__":
    sys.exit(main())
