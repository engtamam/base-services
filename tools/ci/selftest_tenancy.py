#!/usr/bin/env python
"""Mutation proof for safety:tenancy. A gate that cannot fail is decoration.

This does not test the gate's opinions. It writes a service that is CORRECT, confirms the gate
passes it, then breaks it in five specific ways — one per rule — and demands the gate go red for
each. If a mutation survives, the rule it belongs to is not being enforced and the gate is lying.

Run: python tools/ci/selftest_tenancy.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_tenancy.py"

# A minimal, CORRECT identity service: the person carries no authority, the tenant comes off the
# membership, the mint guard refuses, and refresh re-verifies.
GOOD_CORE = '''
from dataclasses import dataclass, field

@dataclass
class Person:
    id: str
    identifier: str
    password_hash: str
    status: str = "active"

@dataclass
class Membership:
    person_id: str
    tenant: str
    caps: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    status: str = "active"

class TenantAccessDenied(Exception):
    pass

def claims_for(person, membership):
    if membership.status != "active":
        raise TenantAccessDenied()
    return {"tenant": membership.tenant, "caps": membership.caps, "roles": membership.roles}

async def authorize_tenant(store, person, requested):
    m = await store.membership(person.id, requested)
    if m is None:
        raise TenantAccessDenied()
    return m

class AuthService:
    async def refresh(self, token):
        m = await authorize_tenant(self._ms, self._person, self._tenant)
        return claims_for(self._person, m)
'''

GOOD_API = '''
from fastapi import APIRouter
router = APIRouter()

@router.post("/tenant")
async def select_tenant(request, body):
    return {}
'''

# Each mutation is a real mistake someone could make on a Friday afternoon, and each one is a
# cross-tenant breach that nothing else in this repository would catch.
MUTATIONS = {
    "R5 person carries caps": (
        "core",
        '    password_hash: str\n    status: str = "active"\n\n@dataclass\nclass Membership:',
        '    password_hash: str\n    caps: list = field(default_factory=list)\n'
        '    status: str = "active"\n\n@dataclass\nclass Membership:',
    ),
    "R1 tenant taken from the request": (
        "core",
        'return {"tenant": membership.tenant,',
        'return {"tenant": body.tenant,',
    ),
    "R3 token carries two tenants": (
        "core",
        'return {"tenant": membership.tenant,',
        'return {"tenant": [membership.tenant, "other"],',
    ),
    "R2 mint guard cannot refuse": (
        "core",
        '    if membership.status != "active":\n        raise TenantAccessDenied()\n',
        '',
    ),
    "R2/T4 refresh stops re-verifying": (
        "core",
        "m = await authorize_tenant(self._ms, self._person, self._tenant)",
        "m = await self._ms.membership(self._person.id, self._tenant)",
    ),
    "R4/T2 a tenant admin resets the person's password": (
        "api",
        "async def select_tenant(request, body):\n    return {}",
        "async def select_tenant(request, body, principal: Principal):\n"
        "    person.password_hash = body.new_password\n    return {}",
    ),
}


def write_tree(root: pathlib.Path, core: str, api: str) -> None:
    svc = root / "identity-service" / "app"
    (svc / "core").mkdir(parents=True, exist_ok=True)
    (svc / "api" / "v1").mkdir(parents=True, exist_ok=True)
    (svc / "core" / "membership.py").write_text(core, encoding="utf-8")
    (svc / "api" / "v1" / "tenancy.py").write_text(api, encoding="utf-8")


def run_gate(services_dir: pathlib.Path) -> int:
    return subprocess.run(
        [sys.executable, str(CHECK)],
        env={"TENANCY_SERVICES_DIR": str(services_dir), "PATH": "", "SYSTEMROOT": ""},
        capture_output=True, text=True,
    ).returncode


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)

        # 1. The gate must PASS correct code. A gate that fails everything is not a gate either.
        clean = base / "clean"
        write_tree(clean, GOOD_CORE, GOOD_API)
        if run_gate(clean) != 0:
            print("  FAIL: the gate rejects CORRECT code — it would block every honest commit")
            return 1
        print("  ok   correct service passes")

        # 2. Every mutation must be CAUGHT.
        for i, (name, (which, old, new)) in enumerate(MUTATIONS.items()):
            core, api = GOOD_CORE, GOOD_API
            if which == "core":
                assert old in core, f"selftest is stale: {name!r} no longer matches the fixture"
                core = core.replace(old, new)
            else:
                assert old in api, f"selftest is stale: {name!r} no longer matches the fixture"
                api = api.replace(old, new)

            tree = base / f"mut{i}"
            write_tree(tree, core, api)
            if run_gate(tree) == 0:
                failures.append(name)
                print(f"  FAIL {name} — SURVIVED. The rule is not enforced.")
            else:
                print(f"  ok   {name} — caught")

    if failures:
        print(f"\nsafety:tenancy selftest FAILED: {len(failures)} mutation(s) survived.")
        print("The gate reports green on code it cannot actually check.")
        return 1
    print(f"\nsafety:tenancy selftest OK — all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
