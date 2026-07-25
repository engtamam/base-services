#!/usr/bin/env python3
"""safety:operator-coverage — a platform capability the code enforces but the manifest hides.

THE FAILURE THIS EXISTS TO PREVENT, found for real in Phase 47.

file-service had a `platform.files` capability: a check deep in the file-access path
(`if "platform.files" in principal.caps: return f`) let a platform operator read any tenant's file
across the boundary. It worked. It was enforced. And it was NOT in the manifest's `capabilities`
list — so no admin panel, no generated client, and no discovery consumer could know the capability
existed. An operator power that nothing advertises is a power only whoever reads the source can use;
to everyone else it is invisible, and an invisible operator surface is the exact half-a-panel this
phase set out to close.

The manifest's `capabilities` is a PROMISE: "these are the platform capabilities this service
recognises." A `platform.*` capability enforced in the code but absent from that promise breaks it —
the code grants a power the service swears it does not grant.

So this gate compares, per service:

    { platform.* capabilities the code ENFORCES }  ⊆  manifest.capabilities

"Enforces" means it appears as a STRING LITERAL in a `require_capability(...)` /
`require_any_capability(...)` dependency, or in a `"platform.x" in <...>.caps` guard inside a
handler — the two ways this codebase gates a route on a capability. Read as source text, never by
importing: a gate that executes what it audits is not a gate.

SCOPE, and why it is drawn here. Only `platform.*` capabilities are required to be advertised —
they are unambiguously the platform-operator surface, and getting one wrong is the file-service bug.
A generic diagnostic cap like bare `admin` (the `/admin/ping` health gate) is deliberately NOT part
of a service's advertised capability surface, so it is not required here. Tenant-scoped grants
(`*.manage`, `*.read`, `members:write`, …) are advertised where they are part of the product surface
but are not forced by this gate, because they are not the cross-tenant operator power whose silent
absence hides a breach.

WHAT IS NOT CHECKED: the reverse direction (an advertised capability nothing enforces). A capability
may be advertised ahead of the route that will use it, or enforced through a settings-derived name
this gate cannot see, so a declared-but-unenforced entry is not automatically wrong. This gate
catches the direction that hides an operator power.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# require_capability("x") / require_any_capability("x", "y") / require_capabilities(...)
_REQUIRE = re.compile(r"require_(?:any_)?capabilit(?:y|ies)?\(([^)]*)\)")
# "platform.x" in principal.caps  /  in p.caps  /  in claims.caps  — the in-handler guard
_IN_CAPS = re.compile(r"""["']([a-z][\w.]*)["']\s+in\s+[\w.]*caps\b""")
# Any single-quoted/double-quoted lowercase-dotted token (a capability literal).
_LITERAL = re.compile(r"""["']([a-z][\w.:]*)["']""")
# The capabilities list literal in manifest.py: "capabilities": [ ... ]
_CAPS_LIST = re.compile(r'"capabilities"\s*:\s*\[([^\]]*)\]', re.S)


def enforced_platform_caps(service: pathlib.Path) -> set[str]:
    """Every `platform.*` capability this service's API layer gates a route on."""
    caps: set[str] = set()
    api = service / "app" / "api"
    if not api.is_dir():
        return caps
    for f in api.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for call in _REQUIRE.finditer(text):
            caps |= set(_LITERAL.findall(call.group(1)))
        caps |= set(_IN_CAPS.findall(text))
    return {c for c in caps if c.startswith("platform.")}


def advertised_caps(service: pathlib.Path) -> set[str]:
    manifest = service / "app" / "manifest.py"
    if not manifest.is_file():
        return set()
    m = _CAPS_LIST.search(manifest.read_text(encoding="utf-8"))
    return set(_LITERAL.findall(m.group(1))) if m else set()


def main() -> int:
    services = sorted(
        p for p in SERVICES.iterdir()
        if p.is_dir() and p.name != "_template" and (p / "app").is_dir())

    problems: list[str] = []
    checked = 0
    for svc in services:
        checked += 1
        hidden = enforced_platform_caps(svc) - advertised_caps(svc)
        for cap in sorted(hidden):
            problems.append(
                f"{svc.name}: enforces {cap!r} but the manifest does not advertise it — "
                f"the code grants a platform power the service swears it does not")

    if problems:
        print(f"safety:operator-coverage — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nAn operator power nothing advertises is one only whoever reads the source can use. "
              "Add the capability to app/manifest.py's `capabilities`, or stop enforcing it.")
        return 1

    print(f"      {checked} services advertise every platform.* capability their code enforces")
    return 0


if __name__ == "__main__":
    sys.exit(main())
