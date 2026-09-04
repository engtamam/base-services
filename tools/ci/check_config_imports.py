#!/usr/bin/env python3
"""safety:config-imports — a config.py that drops a shared module-level import is a NameError waiting
for the one env value that reaches the missing name.

THE FAILURE THIS EXISTS TO PREVENT, hit for real (v0.18.12).

Every service's config.py carries a shared preamble: the same `_json_any_map()` helper that parses
JSON env vars (INTERNAL_CALLER_PUBLIC_KEYS_JSON among them) with `_json.loads`, and the module-level
`import json as _json` it depends on. Three services had that module-level import DROPPED — the only
`import json as _json` left in them sat inside two OTHER functions, invisible to `_json_any_map`. So
the moment a NON-EMPTY pubkey map was parsed (the empty case early-returns before `_json.loads`),
`require_internal_token` crashed with `NameError: name '_json' is not defined` — breaking EVERY
internal-token route on those services. The admin catalog showed "couldn't reach" them; nothing was
wrong with the routes, only a dropped import at module scope.

No test caught it because config.py is not imported-and-exercised by most unit tests, and the crash
needed a specific env value to reach the missing name. So this gate compares each service's config.py
against the TEMPLATE's shared preamble:

    _template/app/config.py  module-level imports  ⊆  every service's config.py module-level imports

A service may ADD imports; it may never DROP one the template established as shared. Read as source
text via ast, never by importing — a gate that executes what it audits is not a gate.

WHAT IS NOT CHECKED: the reverse (a service adding its own imports is fine), and function-local
imports (they are legitimately scoped — the bug was precisely relying on one from the wrong scope).
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
TEMPLATE = SERVICES / "_template" / "app" / "config.py"


def _module_imports(path: pathlib.Path) -> set[tuple]:
    """The imports bound at MODULE scope (top-level body only) — as hashable identity tuples.
    Function-local imports are deliberately excluded: the bug was depending on one of those."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[tuple] = set()
    for node in tree.body:                       # MODULE level only — not walked into functions
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(("import", a.name, a.asname or a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            for a in node.names:
                out.add(("from", mod, a.name, a.asname or a.name))
    return out


def _fmt(imp: tuple) -> str:
    if imp[0] == "import":
        return f"import {imp[1]}" + (f" as {imp[2]}" if imp[2] != imp[1] else "")
    return f"from {imp[1]} import {imp[2]}" + (f" as {imp[3]}" if imp[3] != imp[2] else "")


def main() -> int:
    if not TEMPLATE.is_file():
        print("      no _template/app/config.py — nothing to compare against")
        return 0
    template = _module_imports(TEMPLATE)

    problems: list[str] = []
    checked = 0
    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template"):
        cfg = service / "app" / "config.py"
        if not cfg.is_file():
            continue
        checked += 1
        missing = template - _module_imports(cfg)
        if missing:
            listed = "\n".join(f"        {_fmt(m)}" for m in sorted(missing))
            problems.append(
                f"  {service.name}: config.py dropped {len(missing)} shared module-level import(s):\n"
                f"{listed}\n"
                f"      A module-level helper (e.g. _json_any_map) will NameError the moment an env\n"
                f"      value reaches the missing name — breaking every route that parses it.")

    if problems:
        print("      a config.py drifted below the template's shared imports:")
        print("\n".join(problems))
        return 1
    print(f"      {checked} services' config.py carry every shared import the template establishes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
