#!/usr/bin/env python
"""Gate: an operator's cross-tenant override must actually be WIRED, not designed and left dead.

THE FOOTGUN THIS EXISTS FOR.

Several services resolve "which tenant is this request for?" through a small helper that lets a
PLATFORM operator act on a chosen tenant while a normal tenant admin stays locked to their own:

    def _tenant(principal, body_tenant=None):
        if "platform.compliance" in principal.caps:
            return body_tenant or principal.tenant or "global"   # <- the operator override
        return principal.tenant or "__none__"

The override only works if a route SUPPLIES it — a `tenant` query param, or a `tenant` field on the
body — and passes it in: `_tenant(principal, tenant)`. When every call site is `_tenant(principal)`
(one argument), the override is DEAD: a GET has no body, nothing ever fills `body_tenant`, and a
platform operator is silently scoped to their OWN reserved tenant — unable to view or act on any real
shop's records, platform-wide. It compiles, every test that only uses a tenant admin passes, and the
capability the operator holds does nothing. (Reported live: compliance's admin DSAR/consent/legal-hold
routes.)

THE RULE. In each service, for every helper that looks like an operator-tenant resolver — it reads
`<param>.caps` and `return`s `<override_param> or ...` where `override_param` is one of its own later
parameters — at least one call to it MUST supply that override (a 2nd positional arg, or the keyword).
A resolver whose override NO call site ever passes is a dead override → FAIL.

This is deliberately the CHECKABLE invariant. Whether one specific by-id route (which derives the
tenant from the fetched object) should pass the override is not statically decidable and is left to
review + tests; but "the override exists and nobody ever wires it" is exactly the reported bug, and
that this gate proves cannot happen again.

PLUG-AND-PLAY: the platform-cap marker (`.caps`) and reviewed exemptions are config.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# Reviewed exemptions: "service:function" resolvers deliberately without a wired override.
_EXEMPT = {x.strip() for x in os.environ.get("OP_TENANT_EXEMPT", "").split(",") if x.strip()}


def _override_param(fn: ast.FunctionDef) -> str | None:
    """If `fn` is an operator-tenant resolver, return the name of its override parameter, else None.

    Shape: the body references `<something>.caps` AND has a `return <name> or ...` where `<name>` is
    one of the function's parameters other than the first (the principal)."""
    params = [a.arg for a in fn.args.args]
    if len(params) < 2:
        return None
    later = set(params[1:])
    reads_caps = any(isinstance(n, ast.Attribute) and n.attr == "caps" for n in ast.walk(fn))
    if not reads_caps:
        return None
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.BoolOp) \
                and isinstance(node.value.op, ast.Or) and node.value.values:
            first = node.value.values[0]
            if isinstance(first, ast.Name) and first.id in later:
                return first.id
    return None


def _supplies_override(call: ast.Call, override: str) -> bool:
    """A call supplies the override if it passes a 2nd positional arg or the override keyword."""
    if len(call.args) >= 2:
        return True
    return any(kw.arg == override for kw in call.keywords)


def scan_service(root: pathlib.Path) -> list[str]:
    """Return a list of 'file:line function' dead-override violations for one service."""
    service = root.name
    app = root / "app"
    if not app.is_dir():
        return []
    resolvers: dict[str, tuple[str, str, int]] = {}   # name -> (override_param, rel, lineno)
    calls: dict[str, list[ast.Call]] = {}
    for path in sorted(app.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(ROOT).as_posix() if _under(path, ROOT) else path.as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                ov = _override_param(node)
                if ov is not None:
                    resolvers.setdefault(node.name, (ov, rel, node.lineno))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.setdefault(node.func.id, []).append(node)
    bad: list[str] = []
    for name, (override, rel, lineno) in resolvers.items():
        if f"{service}:{name}" in _EXEMPT:
            continue
        sites = calls.get(name, [])
        if not sites:
            continue                                   # unused resolver: a dead-code concern, not this
        if not any(_supplies_override(c, override) for c in sites):
            bad.append(f"{rel}:{lineno} {name}() — override '{override}' is never supplied by any "
                       f"of its {len(sites)} call site(s)")
    return bad


def _under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main() -> int:
    violations: list[str] = []
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        violations.extend(scan_service(svc))
    if violations:
        print("safety:operator-tenant-wired — a platform operator's cross-tenant override is DEAD (no")
        print("call site ever supplies it), so an operator is silently locked to their own tenant.")
        print("Wire it: pass the tenant (query param / body field) into the resolver, e.g.")
        print("`_tenant(principal, tenant)`. Reviewed exceptions go in OP_TENANT_EXEMPT.")
        for v in violations:
            print(f"  {v}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
