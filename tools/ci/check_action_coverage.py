#!/usr/bin/env python
"""Gate: every user-tier route is either DECLARED to a panel, or explicitly excluded — with a reason.

safety:admin and safety:user-actions prove a declaration is not FALSE (the route exists, the cap is
real). Neither can prove it is COMPLETE. That gap is not theoretical: tenant-service declared six
mutations and no `GET /api/v1/tenants`, so the admin panel could suspend a customer but could not
answer "which customers are there?" — a Customers page was impossible, and every gate stayed green.
Reads are the half of a panel that is easy to forget, because nothing breaks when they are missing.

So this gate inverts the question. For each service it takes every `tier: user` route in the manifest
and requires it to be one of:

  * declared in app/admin_actions.py  (an operator control), or
  * declared in app/user_actions.py   (an end-user control), or
  * listed in UNDECLARED_ROUTES in app/user_actions.py, WITH a `why`.

That last one is the point: an omission stops being an oversight and becomes a reviewed decision a
human wrote a reason for. Pre-auth flows and programmatic (SDK/service-to-service) endpoints are
legitimate exclusions; a forgotten list endpoint is not.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("COVERAGE_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_BRACE = re.compile(r"\{[^}]*\}")


def _norm(path: str) -> str:
    return _BRACE.sub("{}", path)


def _literal(path: pathlib.Path, name: str):
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    target, value = t, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            target, value = node.target, node.value
        if target is not None and value is not None:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return []
    return []


def _user_tier_routes(svc: pathlib.Path) -> set[tuple[str, str]]:
    man = svc / "app" / "manifest.py"
    out: set[tuple[str, str]] = set()
    if not man.is_file():
        return out
    try:
        tree = ast.parse(man.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "endpoints" \
                        and isinstance(v, (ast.List, ast.Tuple)):
                    for e in v.elts:
                        if isinstance(e, ast.Dict):
                            d = {kk.value: vv.value for kk, vv in zip(e.keys, e.values)
                                 if isinstance(kk, ast.Constant) and isinstance(vv, ast.Constant)}
                            if d.get("tier") == "user" and "method" in d and "path" in d:
                                out.add((d["method"], d["path"]))
    return out


def check_service(svc: pathlib.Path, problems: list[str]) -> int:
    routes = _user_tier_routes(svc)
    if not routes:
        return 0
    declared: set[tuple[str, str]] = set()
    for fname, lname in (("admin_actions.py", "ADMIN_ACTIONS"),
                         ("user_actions.py", "USER_ACTIONS")):
        for a in _literal(svc / "app" / fname, lname):
            if isinstance(a, dict) and isinstance(a.get("method"), str) and isinstance(a.get("path"), str):
                declared.add((a["method"], _norm(a["path"])))

    excluded: set[tuple[str, str]] = set()
    for x in _literal(svc / "app" / "user_actions.py", "UNDECLARED_ROUTES"):
        if not isinstance(x, dict):
            problems.append(f"{svc.name}: an UNDECLARED_ROUTES entry is not an object")
            continue
        m, p, why = x.get("method"), x.get("path"), x.get("why")
        if not (isinstance(m, str) and isinstance(p, str)):
            problems.append(f"{svc.name}: UNDECLARED_ROUTES entry needs method+path")
            continue
        if not (isinstance(why, str) and why.strip()):
            problems.append(f"{svc.name}: {m} {p} is excluded without a `why` — an omission must "
                            f"carry a reason, or it is just an oversight")
            continue
        excluded.add((m, _norm(p)))

    missing = sorted((m, p) for (m, p) in routes
                     if (m, _norm(p)) not in declared and (m, _norm(p)) not in excluded)
    for m, p in missing:
        problems.append(f"{svc.name}: {m} {p} is a user-tier route but is neither declared to a "
                        f"panel nor listed in UNDECLARED_ROUTES with a reason")
    return len(routes)


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    checked = covered = 0
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir() or svc.name == "_template":
            continue
        checked += 1
        covered += check_service(svc, problems)
    if not checked:
        print("FATAL: no services found — a gate with no subject cannot pass")
        return 1
    if problems:
        print(f"safety:action-coverage — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nA panel that can only ACT is half a panel. Declare the route (usually a read), or "
              "record why it is deliberately not a control.")
        return 1
    print(f"safety:action-coverage OK — {covered} user-tier route(s) across {checked} service(s); "
          f"every one is declared to a panel or excluded with a stated reason")
    return 0


if __name__ == "__main__":
    sys.exit(main())
