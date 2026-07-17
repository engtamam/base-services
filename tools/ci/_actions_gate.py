#!/usr/bin/env python
"""Shared core for the declared-actions gates (safety:admin and safety:user-actions).

Both panels work the same way: each service declares actions as a PURE literal in a module, and its
manifest emits them under a key. The panel aggregates and renders forms; a declaration that lies (a
route that does not exist, a capability the route never checks, a scheme/host in the path) makes the
panel misrepresent what a caller may do, or opens an SSRF/redirect. So every declaration is checked
against the CODE, with the same five rules (+ R6, the manifest actually emits it):

  R1 (method, path) exists in the manifest's `endpoints`.   R4 id/label/capability well-formed.
  R2 path is RELATIVE (no scheme/host/'//').                R5 capability is manifest-advertised.
  R3 method is allow-listed.                                R6 the manifest emits the field.

`admin_actions` (operator panel) and `user_actions` (end-user app) are the same shape and rules; only
the module name, literal name, and manifest key differ. This module is that shared checker.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
from dataclasses import dataclass

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/{}-]*$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_CAP_RE = re.compile(r"^[a-z][a-z0-9_.:*-]{1,63}$")
_BRACE = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class GateSpec:
    gate_name: str          # "safety:admin"
    noun: str               # "admin actions"
    surface: str            # "panel" | "app"
    literal_name: str       # "ADMIN_ACTIONS"
    decl_filename: str      # "admin_actions.py"
    manifest_field: str     # "admin_actions"
    root_env: str           # "ADMIN_GATE_ROOT"
    tail: str               # closing sentence on failure


def _norm(path: str) -> str:
    return _BRACE.sub("{}", path)


def _literal_from_module(path: pathlib.Path, name: str):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
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
                return None
    return None


def _manifest_emits(svc: pathlib.Path, field: str) -> bool:
    man = svc / "app" / "manifest.py"
    if not man.is_file():
        return False
    try:
        tree = ast.parse(man.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value == field:
                    return True
    return False


def _manifest_facts(svc: pathlib.Path) -> tuple[set[tuple[str, str]], set[str]]:
    man = svc / "app" / "manifest.py"
    routes: set[tuple[str, str]] = set()
    caps: set[str] = set()
    if not man.is_file():
        return routes, caps
    try:
        tree = ast.parse(man.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return routes, caps
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if not isinstance(k, ast.Constant):
                    continue
                if k.value == "endpoints" and isinstance(v, (ast.List, ast.Tuple)):
                    for e in v.elts:
                        if isinstance(e, ast.Dict):
                            d = {kk.value: vv.value for kk, vv in zip(e.keys, e.values)
                                 if isinstance(kk, ast.Constant) and isinstance(vv, ast.Constant)}
                            if "method" in d and "path" in d:
                                routes.add((d["method"], _norm(d["path"])))
                if k.value == "capabilities" and isinstance(v, (ast.List, ast.Tuple)):
                    for c in v.elts:
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            caps.add(c.value)
    return routes, caps


def _check_service(spec: GateSpec, svc: pathlib.Path, problems: list[str]) -> None:
    decl = svc / "app" / spec.decl_filename
    if not decl.is_file():
        return                              # opt-in
    actions = _literal_from_module(decl, spec.literal_name)
    if actions is None:
        problems.append(f"{svc.name}: app/{spec.decl_filename} has no static {spec.literal_name} list")
        return
    if not _manifest_emits(svc, spec.manifest_field):
        problems.append(f"{svc.name}: declares {spec.manifest_field} but manifest.py never emits a "
                        f"'{spec.manifest_field}' key — the {spec.surface} would show nothing (a false green)")
        return
    routes, caps = _manifest_facts(svc)
    seen_ids: set[str] = set()
    for a in actions:
        if not isinstance(a, dict):
            problems.append(f"{svc.name}: an action is not an object"); continue
        aid = a.get("id", "?")
        if not (isinstance(aid, str) and _ID_RE.match(aid)):
            problems.append(f"{svc.name}: action id {aid!r} is malformed"); continue
        if aid in seen_ids:
            problems.append(f"{svc.name}: duplicate action id {aid!r}")
        seen_ids.add(aid)
        if not (isinstance(a.get("label"), str) and a["label"].strip()):
            problems.append(f"{svc.name}:{aid}: missing label")
        method, path, cap = a.get("method"), a.get("path"), a.get("capability")
        if method not in _METHODS:
            problems.append(f"{svc.name}:{aid}: method {method!r} not in {sorted(_METHODS)}"); continue
        if not (isinstance(path, str) and _PATH_RE.match(path)) or (isinstance(path, str) and path.startswith("//")):
            problems.append(f"{svc.name}:{aid}: path {path!r} must be relative (no scheme/host/'//')")
            continue
        if not (isinstance(cap, str) and _CAP_RE.match(cap)):
            problems.append(f"{svc.name}:{aid}: capability {cap!r} is malformed"); continue
        if (method, _norm(path)) not in routes:
            problems.append(f"{svc.name}:{aid}: declares {method} {path} but the manifest lists no "
                            f"such route — a phantom action the {spec.surface} would render")
        if caps and cap not in caps:
            problems.append(f"{svc.name}:{aid}: capability {cap!r} is not in the service's advertised "
                            f"capabilities {sorted(caps)} — the {spec.surface} would gate on a cap the "
                            f"service never checks")


def run(spec: GateSpec) -> int:
    root = pathlib.Path(os.environ.get(spec.root_env, "") or pathlib.Path(__file__).resolve().parents[2])
    services = root / "services"
    if not services.is_dir():
        print(f"FATAL: no services/ under {root}")
        return 1
    problems: list[str] = []
    checked = 0
    for svc in sorted(services.iterdir()):
        if not svc.is_dir() or svc.name == "_template":
            continue
        checked += 1
        _check_service(spec, svc, problems)
    if not checked:
        print("FATAL: no services found — a gate with no subject cannot pass")
        return 1
    if problems:
        print(f"{spec.gate_name} — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print(f"\n{spec.tail}")
        return 1
    declared = sum(1 for s in services.iterdir() if (s / "app" / spec.decl_filename).is_file())
    print(f"{spec.gate_name} OK — {declared} service(s) declare {spec.noun}; every one maps to a "
          f"real route with a real capability, and no declaration carries a scheme/host")
    return 0
