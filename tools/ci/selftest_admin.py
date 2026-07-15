#!/usr/bin/env python
"""Mutation proof for safety:admin. A gate that cannot fail is decoration.

Writes a CORRECT service (manifest + admin_actions), confirms it passes, then breaks the declaration
one rule at a time and demands the gate go red each time.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_admin.py"

MANIFEST = '''
from typing import Any
def build_manifest(settings=None, ports=None):
    return {
        "name": "x-service",
        "capabilities": ["platform.x"],
        "endpoints": [
            {"path": "/api/v1/x/{id}/suspend", "tier": "user", "method": "POST"},
        ],
        "admin_actions": [],
    }
'''

GOOD_ACTIONS = '''
ADMIN_ACTIONS = [
    {"id": "x.suspend", "label": "Suspend", "method": "POST",
     "path": "/api/v1/x/{id}/suspend", "capability": "platform.x", "danger": True},
]
'''

MUTATIONS = {
    "R1 phantom route": GOOD_ACTIONS.replace("/api/v1/x/{id}/suspend", "/api/v1/x/{id}/nuke"),
    "R2 absolute-url path": GOOD_ACTIONS.replace('"/api/v1/x/{id}/suspend"', '"http://evil/x"'),
    "R3 bad method": GOOD_ACTIONS.replace('"POST"', '"CONNECT"'),
    "R4 malformed id": GOOD_ACTIONS.replace('"x.suspend"', '"X SUSPEND!"'),
    "R5 cap not advertised": GOOD_ACTIONS.replace('"platform.x"', '"platform.secret"'),
}


def write(base: pathlib.Path, actions: str) -> None:
    app = base / "services" / "x-service" / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "manifest.py").write_text(MANIFEST, encoding="utf-8")
    (app / "admin_actions.py").write_text(actions, encoding="utf-8")


def run_gate(base: pathlib.Path) -> int:
    return subprocess.run([sys.executable, str(CHECK)],
                          env={**os.environ, "ADMIN_GATE_ROOT": str(base)},
                          capture_output=True, text=True).returncode


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        write(base, GOOD_ACTIONS)
        if run_gate(base) != 0:
            print("  FAIL: the gate rejects a CORRECT declaration"); return 1
        print("  ok   correct declaration passes")

        for name, mutated in MUTATIONS.items():
            write(base, mutated)
            if run_gate(base) == 0:
                failures.append(name); print(f"  FAIL {name} — SURVIVED")
            else:
                print(f"  ok   {name} — caught")

        # R6: valid declaration, but the manifest never emits the admin_actions key (false green).
        app = base / "services" / "x-service" / "app"
        (app / "admin_actions.py").write_text(GOOD_ACTIONS, encoding="utf-8")
        (app / "manifest.py").write_text(MANIFEST.replace('"admin_actions": [],', ""), encoding="utf-8")
        if run_gate(base) == 0:
            failures.append("R6 manifest omits admin_actions"); print("  FAIL R6 manifest omits admin_actions — SURVIVED")
        else:
            print("  ok   R6 manifest omits admin_actions — caught")

    if failures:
        print(f"\nsafety:admin selftest FAILED: {len(failures)} mutation(s) survived.")
        return 1
    print("\nsafety:admin selftest OK — every mutation caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
