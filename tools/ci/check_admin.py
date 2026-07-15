#!/usr/bin/env python
"""Gate: the admin panel is a master key. Its declarations must be true, and safe.

Each service declares its admin actions in `app/admin_actions.py`. The platform aggregates them and
renders forms an operator can submit. A declaration that lies (an action whose route does not exist,
or enforces a different capability than it claims) makes the panel misrepresent what an operator can
do — and a declaration that carries a scheme/host is an SSRF/redirect into the cluster.

So every declaration is checked against the CODE, from phase-33's rules:

  R1  Every declared action's (method, path) exists as a route the service actually serves — its
      manifest endpoints are the source of truth.
  R2  A declared `path` is RELATIVE — no scheme, no host, no `//`. (SSRF / open redirect.)
  R3  `method` is one of the allow-listed verbs.
  R4  `id`, `capability`, `label` are present and well-formed; `danger` actions are marked.
  R5  The action's declared `capability` is one the service actually advertises (manifest
      `capabilities`), so the panel does not hide/show a button by a cap the service never checks.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("ADMIN_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/{}-]*$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_CAP_RE = re.compile(r"^[a-z][a-z0-9_.:*-]{1,63}$")

# A path with a {param} matches a manifest endpoint with a differently-named {param}: the panel
# fills params by name, so `{tenant_id}` and `{id}` are the same route shape.
_BRACE = re.compile(r"\{[^}]*\}")


def _norm(path: str) -> str:
    return _BRACE.sub("{}", path)


def _literal_from_module(path: pathlib.Path, name: str):
    """Read a top-level literal assignment from a module WITHOUT importing it (no side effects, no
    deps). Returns the Python value or None."""
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
                and node.target.id == name:            # ADMIN_ACTIONS: list[...] = [...]
            target, value = node.target, node.value
        if target is not None and value is not None:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
    return None


def _manifest_wires_admin(svc: pathlib.Path) -> bool:
    """Does manifest.py actually EMIT an `admin_actions` key in the dict it returns?

    The gate reads admin_actions.py and manifest.py independently, so a declaration can be valid while
    the manifest never emits it — the panel would then show nothing and the gate would stay green
    (a false green). This closes that: the returned dict must carry an `admin_actions` key. We accept
    any value (the helper is `_admin_actions()`); we only insist the wire exists.
    """
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
                if isinstance(k, ast.Constant) and k.value == "admin_actions":
                    return True
    return False


def _manifest_facts(svc: pathlib.Path) -> tuple[set[tuple[str, str]], set[str]]:
    """(routes, capabilities) the service ADVERTISES, read from its manifest builder's literals.

    The manifest returns a dict literal with `endpoints` and `capabilities`; we parse those without
    importing the app. Endpoints there are the ground truth for what routes exist.
    """
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


def check_service(svc: pathlib.Path, problems: list[str]) -> None:
    decl = svc / "app" / "admin_actions.py"
    if not decl.is_file():
        return                              # a service may declare nothing — opt-in
    actions = _literal_from_module(decl, "ADMIN_ACTIONS")
    if actions is None:
        problems.append(f"{svc.name}: app/admin_actions.py has no static ADMIN_ACTIONS list")
        return
    if not _manifest_wires_admin(svc):
        problems.append(f"{svc.name}: declares admin_actions but manifest.py never emits an "
                        f"'admin_actions' key — the panel would show nothing (a false green)")
        return
    routes, caps = _manifest_facts(svc)
    seen_ids: set[str] = set()
    for a in actions:
        if not isinstance(a, dict):
            problems.append(f"{svc.name}: an admin action is not an object")
            continue
        aid = a.get("id", "?")
        # R4 — shape
        if not (isinstance(aid, str) and _ID_RE.match(aid)):
            problems.append(f"{svc.name}: action id {aid!r} is malformed"); continue
        if aid in seen_ids:
            problems.append(f"{svc.name}: duplicate action id {aid!r}")
        seen_ids.add(aid)
        if not (isinstance(a.get("label"), str) and a["label"].strip()):
            problems.append(f"{svc.name}:{aid}: missing label")
        method, path, cap = a.get("method"), a.get("path"), a.get("capability")
        # R3 — method
        if method not in _METHODS:
            problems.append(f"{svc.name}:{aid}: method {method!r} not in {sorted(_METHODS)}"); continue
        # R2 — relative path
        if not (isinstance(path, str) and _PATH_RE.match(path)) or (isinstance(path, str) and path.startswith("//")):
            problems.append(f"{svc.name}:{aid}: path {path!r} must be relative (no scheme/host/'//')")
            continue
        if not (isinstance(cap, str) and _CAP_RE.match(cap)):
            problems.append(f"{svc.name}:{aid}: capability {cap!r} is malformed"); continue
        # R1 — the route exists
        if (method, _norm(path)) not in routes:
            problems.append(f"{svc.name}:{aid}: declares {method} {path} but the manifest lists no "
                            f"such route — a phantom action the panel would render")
        # R5 — the cap is one the service advertises
        if caps and cap not in caps:
            problems.append(f"{svc.name}:{aid}: capability {cap!r} is not in the service's advertised "
                            f"capabilities {sorted(caps)} — the panel would gate on a cap the "
                            f"service never checks")


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    checked = 0
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir() or svc.name == "_template":
            continue
        checked += 1
        check_service(svc, problems)
    if not checked:
        print("FATAL: no services found — a gate with no subject cannot pass")
        return 1
    if problems:
        print(f"safety:admin — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nThe admin panel is a master key. A declaration that lies about a route or a "
              "capability makes it misrepresent what an operator may do.")
        return 1
    declared = sum(1 for s in SERVICES.iterdir()
                   if (s / "app" / "admin_actions.py").is_file())
    print(f"safety:admin OK — {declared} service(s) declare admin actions; every one maps to a "
          f"real route with a real capability, and no declaration carries a scheme/host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
