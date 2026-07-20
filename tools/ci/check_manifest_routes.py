#!/usr/bin/env python3
"""safety:manifest-routes — a route the manifest does not admit to is a route nobody can discover.

THE FAILURE THIS EXISTS TO PREVENT, reported by a team that hit it for real.

Every service serves POST /internal/privacy/erase and ships an app/core/erasure.py. Eighteen of
twenty did not LIST it in app/manifest.py. A team building a DSAR fan-out did the correct thing —
enumerated the holders of personal data from the platform's own discovery surface — and got a map
that did not admit to holding anyone. Their fan-out reached four product services and skipped
identity-service, which owns the only mapping from a subject_ref back to a human.

Then the part that makes it dangerous. Completion is DERIVED from the acks received. A holder that
was never registered is never asked, never acks, and is therefore never MISSED. The request closed
GREEN and the subject was told they had been forgotten while their identity row sat intact.

Nothing disagreed with itself. Every record was internally consistent and collectively false.

So this gate compares the manifest against the ROUTES THE CODE ACTUALLY REGISTERS:

    manifest.endpoints  ⊇  the /internal and /api routes in app/api/**

Read as source text, never by importing — a gate that executes what it audits is not a gate.
Decorator paths are matched against the router prefix, and a path present with a different
placeholder spelling ({id} vs {dsar_id}) is treated as declared, because the shape is what a
discovery consumer needs and the parameter name is ours to choose.

WHAT IS NOT CHECKED, and why saying so matters: the reverse direction. A manifest may legitimately
declare a route that a build flag disables (multi-tenant routers exist only when
IDENTITY_MULTI_TENANT is on), so a declared-but-absent entry is not automatically wrong. This gate
catches the direction that hides data.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# Captures the VARIABLE too. A module routinely declares several routers with different
# prefixes (a public one with none, a members one under another path), so a single
# "the prefix of this file" is wrong — and wrong in the direction that invents routes
# nobody serves, which is how a gate starts crying wolf.
_ROUTER = re.compile(r"""(\w+)\s*=\s*APIRouter\(([^)]*)\)""", re.S)
_PREFIX = re.compile(r"""prefix\s*=\s*["']([^"']*)["']""")
_ROUTE = re.compile(r"""@(\w+)\.(get|post|put|patch|delete)\(\s*["']([^"']*)["']""")
_MANIFEST = re.compile(r'\{"path":\s*"([^"]+)"[^}]*?"method":\s*"(\w+)"', re.S)


def _norm(path: str) -> str:
    """Collapse placeholder NAMES so {id} and {dsar_id} compare equal — a discovery consumer needs
    the shape, and the parameter name is an implementation choice."""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/")) or "/"


def routes_of(service: pathlib.Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    api = service / "app" / "api"
    if not api.is_dir():
        return found
    for path in sorted(api.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # variable -> its own prefix. A router declared without one mounts at root.
        prefixes: dict[str, str] = {}
        for var, args in _ROUTER.findall(text):
            m = _PREFIX.search(args)
            prefixes[var] = m.group(1) if m else ""
        for var, method, route in _ROUTE.findall(text):
            full = (prefixes.get(var, "") + route) or "/"
            if not full.startswith(("/internal", "/api")):
                continue          # health/metrics are public plumbing, declared separately
            found.add((method.upper(), _norm(full)))
    return found


def declared_of(service: pathlib.Path) -> set[tuple[str, str]]:
    mf = service / "app" / "manifest.py"
    if not mf.exists():
        return set()
    return {(m.upper(), _norm(p)) for p, m in _MANIFEST.findall(mf.read_text(encoding="utf-8"))}


def main() -> int:
    problems: list[str] = []
    checked = 0
    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        served, declared = routes_of(service), declared_of(service)
        if not served:
            continue
        checked += 1
        missing = sorted(served - declared)
        if missing:
            listed = "\n".join(f"        {m} {p}" for m, p in missing[:12])
            more = f"\n        … and {len(missing) - 12} more" if len(missing) > 12 else ""
            problems.append(
                f"  {service.name}: serves {len(missing)} route(s) the manifest does not declare:\n"
                f"{listed}{more}\n"
                f"      A consumer building a data map or a route table from /internal/manifest\n"
                f"      cannot see these. For an erasure endpoint that means the holder is never\n"
                f"      asked, never acks, and is therefore never missed.")

    if problems:
        print("      the manifest does not admit to routes the service serves:")
        print("\n".join(problems))
        return 1
    print(f"      {checked} services declare every /internal and /api route they serve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
