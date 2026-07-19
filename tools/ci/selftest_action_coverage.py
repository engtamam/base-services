#!/usr/bin/env python
"""Mutation proof for safety:action-coverage. A gate that cannot fail is decoration.

Writes a service whose every user-tier route is accounted for, confirms it passes, then reproduces
the exact bug this gate exists for — a route that is served but declared nowhere (the missing
`GET /api/v1/tenants`) — and an exclusion with no reason, and demands the gate go red for each.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_action_coverage.py"

MANIFEST = '''
def build_manifest(settings=None, ports=None):
    return {
        "name": "x-service",
        "capabilities": ["platform.x"],
        "endpoints": [
            {"path": "/api/v1/things", "tier": "user", "method": "GET"},
            {"path": "/api/v1/things/{id}/do", "tier": "user", "method": "POST"},
            {"path": "/api/v1/things/ingest", "tier": "user", "method": "POST"},
            {"path": "/internal/x", "tier": "internal", "method": "POST"},
        ],
        "admin_actions": [],
        "user_actions": [],
    }
'''

ADMIN = '''
ADMIN_ACTIONS = [
    {"id": "x.list", "label": "List", "method": "GET",
     "path": "/api/v1/things", "capability": "platform.x"},
    {"id": "x.do", "label": "Do", "method": "POST",
     "path": "/api/v1/things/{id}/do", "capability": "platform.x"},
]
'''

# The third route is excluded WITH a reason -> complete.
USER_OK = '''
USER_ACTIONS = []
UNDECLARED_ROUTES = [
    {"method": "POST", "path": "/api/v1/things/ingest", "why": "SDK firehose, not a button"},
]
'''
# The bug this gate exists for: a served route declared nowhere at all.
USER_MISSING = '''
USER_ACTIONS = []
UNDECLARED_ROUTES = []
'''
# An exclusion with no reason is just an oversight with extra steps.
USER_NO_WHY = '''
USER_ACTIONS = []
UNDECLARED_ROUTES = [
    {"method": "POST", "path": "/api/v1/things/ingest"},
]
'''


def write(base: pathlib.Path, user_src: str, admin_src: str = ADMIN) -> None:
    app = base / "services" / "x-service" / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "manifest.py").write_text(MANIFEST, encoding="utf-8")
    (app / "admin_actions.py").write_text(admin_src, encoding="utf-8")
    (app / "user_actions.py").write_text(user_src, encoding="utf-8")


def run_gate(base: pathlib.Path) -> int:
    return subprocess.run([sys.executable, str(CHECK)],
                          env={**os.environ, "COVERAGE_GATE_ROOT": str(base)},
                          capture_output=True, text=True).returncode


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)

        write(base, USER_OK)
        if run_gate(base) != 0:
            print("  FAIL: the gate rejects a fully-covered service"); return 1
        print("  ok   fully-covered service passes")

        for name, user_src, admin_src in (
            ("undeclared route (the tenant.list bug)", USER_MISSING, ADMIN),
            ("exclusion without a reason", USER_NO_WHY, ADMIN),
            ("a declared READ removed", USER_OK,
             ADMIN.replace('{"id": "x.list", "label": "List", "method": "GET",\n'
                           '     "path": "/api/v1/things", "capability": "platform.x"},\n', "")),
        ):
            write(base, user_src, admin_src)
            if run_gate(base) == 0:
                failures.append(name); print(f"  FAIL {name} — SURVIVED")
            else:
                print(f"  ok   {name} — caught")

    if failures:
        print(f"\nsafety:action-coverage selftest FAILED: {len(failures)} mutation(s) survived.")
        return 1
    print("\nsafety:action-coverage selftest OK — every mutation caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
