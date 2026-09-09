#!/usr/bin/env python
"""Gate: a tenant-scoped admin must not act on ANOTHER tenant's resource by id (IDOR).

THE FOOTGUN THIS EXISTS FOR.

A mutating route addressed by a resource id (`/payments/{payment_id}/refund`) gated on a capability a
TENANT admin can hold (billing.manage, compliance.manage, …) fetches that resource BY ID and mutates
it. If it never checks the resource belongs to the caller's tenant, tenant A refunds tenant B's
payment, releases B's legal hold, advances B's DSAR. The sibling routes right next to it call the
tenant guard (`_guard(principal, obj.tenant)`); the one that forgot is invisible — it compiles, it
passes its own tests, and it is a cross-tenant money/compliance breach. This was found by an audit
reading routes one at a time. It must be checked against the CODE.

So, for every service, for each MUTATING route (post/put/patch/delete) whose path has a resource-id
parameter and whose auth dependency is TENANT-SCOPED (not platform-only):

  the handler body MUST apply a tenant-ownership guard — a call to a `*guard*` helper, or an inline
  comparison against a resource's `.tenant`. A platform-ONLY route (its dependency requires only
  `platform.*` caps) is deliberately cross-tenant and exempt. A genuine, reviewed exception is named
  in PII-style allowlist config, never guessed.

PLUG-AND-PLAY: the platform-cap prefix, the guard-name patterns, and the exemptions are all config, so
a repo whose platform namespace or guard helper is named differently points this gate at itself.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# A capability namespace that means "platform operator" — a dependency requiring ONLY these is a
# deliberate cross-tenant surface and is exempt. Everything else is tenant-scopable and must guard.
_PLATFORM_PREFIXES = tuple(x.strip() for x in
                           os.environ.get("TENANT_GUARD_PLATFORM_PREFIXES", "platform.").split(",")
                           if x.strip())
# A body applies a guard if it CALLS something matching this (…guard…), or compares a `.tenant`.
_GUARD_NAME_RE = re.compile(os.environ.get(
    "TENANT_GUARD_FUNC_RE", r"guard|scope|owned|require_tenant|_load_scoped"))
# Reviewed exceptions: "service:function" (a route that legitimately acts on a GLOBAL, non-tenant
# resource, so there is no per-tenant ownership to guard). Extend via env, comma-separated.
#   compliance-service:instantiate_legal_template — {template_id} is a GLOBAL PDPL template registry
#     key; the route renders it INTO the caller's own tenant. No cross-tenant resource is reached.
_EXEMPT = {"compliance-service:instantiate_legal_template"}
_EXEMPT |= {x.strip() for x in os.environ.get("TENANT_GUARD_EXEMPT", "").split(",") if x.strip()}

_MUTATING = {"post", "put", "patch", "delete"}
_ID_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*id[a-zA-Z0-9_]*)\}")   # {payment_id}, {id}, {sub_id}


def _cap_deps(tree: ast.AST) -> dict[str, bool]:
    """name -> is_platform_only, for every `X = require_(any_)capability(...)` at module scope."""
    out: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        fname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if "require_capability" not in fname and "require_any_capability" not in fname:
            continue
        caps = [a.value for a in node.value.args if isinstance(a, ast.Constant)
                and isinstance(a.value, str)]
        if not caps:
            continue
        platform_only = all(any(c.startswith(p) for p in _PLATFORM_PREFIXES) for c in caps)
        for t in node.targets:
            if isinstance(t, ast.Name):
                out[t.id] = platform_only
    return out


def _dep_name(func: ast.AST) -> str | None:
    """The Depends(NAME) used for the handler's Principal arg, if any."""
    args = func.args
    for arg, default in zip(reversed(args.args), reversed(args.defaults or [])):
        if (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id == "Depends" and default.args
                and isinstance(default.args[0], ast.Name)):
            return default.args[0].id
    return None


def _route_methods_and_path(func: ast.AST) -> tuple[set[str], str, list[str]]:
    """(methods, path, decorator-level dependency names). Auth can live in the DECORATOR's
    dependencies=[Depends(_ADMIN)] just as validly as in a param — miss that and a route gated only
    there looks unguarded-but-unauth'd and is skipped (the run_cycle/run_overage false-negative)."""
    methods, path, dec_deps = set(), "", []
    for dec in func.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr.lower() in _MUTATING):
            methods.add(dec.func.attr.lower())
            if dec.args and isinstance(dec.args[0], ast.Constant):
                path = str(dec.args[0].value)
            for kw in dec.keywords:
                if kw.arg == "dependencies" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for el in kw.value.elts:
                        if (isinstance(el, ast.Call) and isinstance(el.func, ast.Name)
                                and el.func.id == "Depends" and el.args
                                and isinstance(el.args[0], ast.Name)):
                            dec_deps.append(el.args[0].id)
    return methods, path, dec_deps


# The caller's identity flowing into a resource op — principal / p / a tenant expression / a
# _tenant(...) helper. Its presence ALONGSIDE the resource id is the scope-by-query guarantee.
_OWNER_RE = re.compile(r"\bprincipal\b|\bp\b|tenant", re.I)
# Match an AUDIT/LOG call precisely — the method name, not a substring ("record" must not swallow
# "_load_recording", which is a real ownership-scoping call).
_AUDIT_RE = re.compile(r"\.record\b|auditor|logg|\.emit\b|\.warning\b|\.error\b|\.info\b|\.debug\b", re.I)


def _has_guard(func: ast.AST, src: str, id_params: set[str]) -> bool:
    """The route scopes to the caller's tenant if EITHER (a) it calls a *guard/owned/scoped* helper or
    compares a `.tenant`, OR (b) it passes the resource id AND the caller's identity (principal / a
    tenant expression) into the SAME non-audit call — i.e. the fetch/mutation is tenant-filtered (or a
    helper like _owned(svc, id, principal) / _transition(req, id, principal) does the check), so a
    wrong-tenant id resolves to nothing."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fseg = ast.get_source_segment(src, node.func) or ""
            if _GUARD_NAME_RE.search(fseg):
                return True
            # An audit/log call names both the id and the tenant but does NOT scope the resource — it
            # must never count as a guard (that false-negative once hid the real refund IDOR).
            if _AUDIT_RE.search(fseg):
                continue
            # AST-walk the args (robust to compound exprs like `p.tenant or ""` that
            # get_source_segment can miss): does the SAME call carry the resource id AND the caller's
            # identity (a *tenant* name/attr, or principal/p)?
            args = [*node.args, *[k.value for k in node.keywords if k.value is not None]]
            has_id = has_owner = False
            for a in args:
                for sub in ast.walk(a):
                    if isinstance(sub, ast.Name):
                        if sub.id in id_params:
                            has_id = True
                        if sub.id in ("principal", "p") or "tenant" in sub.id.lower():
                            has_owner = True
                    elif isinstance(sub, ast.Attribute) and "tenant" in sub.attr.lower():
                        has_owner = True
            if any(k.arg == "tenant" for k in node.keywords):
                has_owner = True
            if has_id and has_owner:
                return True
        if isinstance(node, ast.Compare):                     # inline `x.tenant != principal.tenant`
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Attribute) and side.attr == "tenant":
                    return True
    return False


def scan_service(svc: pathlib.Path) -> list[str]:
    problems: list[str] = []
    api = svc / "app" / "api"
    if not api.is_dir():
        return problems
    for path in sorted(api.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        deps = _cap_deps(tree)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            methods, route, dec_deps = _route_methods_and_path(func)
            id_params = set(_ID_PARAM_RE.findall(route))
            if not methods or not id_params:
                continue                                       # not a mutating id-addressed route
            # Auth deps from BOTH the param default and the decorator's dependencies=[...].
            route_deps = [d for d in ([_dep_name(func)] + dec_deps) if d and d in deps]
            if not route_deps:
                continue                                       # no cap dependency we can classify
            tenant_scoped = any(not deps[d] for d in route_deps)
            if not tenant_scoped:
                continue                                       # every dep is platform-only ⇒ exempt
            if f"{svc.name}:{func.name}" in _EXEMPT:
                continue
            if not _has_guard(func, src, id_params):
                shown = ", ".join(sorted(set(route_deps)))
                problems.append(
                    f"{svc.name}: {path.relative_to(svc).as_posix()}:{func.lineno} {func.name}() "
                    f"({'/'.join(sorted(methods))} {route}) is tenant-scopable (Depends({shown})) and "
                    f"acts on a resource by id but applies NO tenant-ownership guard — a tenant admin "
                    f"can reach another tenant's resource (IDOR).")
    return problems


def main() -> int:
    problems: list[str] = []
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "app").is_dir())
    for svc in services:
        problems.extend(scan_service(svc))
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\ntenant-guard: FAIL — {len(problems)} id-addressed route(s) can cross tenants")
        return 1
    print(f"tenant-guard: PASS — every tenant-scopable id-addressed mutating route guards ownership")
    return 0


if __name__ == "__main__":
    sys.exit(main())
