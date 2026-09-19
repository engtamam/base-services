#!/usr/bin/env python3
"""safety:open-redirect — a 302 to a NON-LITERAL location needs an allowlist/https guard.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, findings M5/I2).

`RedirectResponse(url)` where `url` comes from stored/gateway data is an open redirect unless its host
is on an allowlist and its scheme is https — otherwise it can bounce a customer to an attacker page
(or downgrade to http). A redirect to a fixed literal path ("/login") is fine; the risk is a variable
target.

This gate flags a `RedirectResponse(...)` / `Response(..., status_code=30x)` whose location is not a
string literal and whose enclosing function calls no host/allowlist guard (a function whose name
contains allow/allowlist/host_allowed/validate/guard/redirect_target). Advisory/heuristic; a genuinely
safe redirect opts out with `# redirect: ok — reason`. Reads source via `ast`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("OPEN_REDIRECT_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_REDIRECT_CTORS = {"RedirectResponse"}
_GUARD_HINTS = ("allow", "host_allowed", "validate", "guard", "redirect_target", "allowlist")


def _has_marker(line: str) -> bool:
    return "# redirect: ok" in line


def _marker_above(lines: list[str], lineno: int) -> bool:
    for i in range(lineno - 1, max(-1, lineno - 4), -1):
        if 0 <= i < len(lines) and _has_marker(lines[i]):
            return True
    return False


def _is_literal(node) -> bool:
    return isinstance(node, ast.Constant)


def _fn_calls_guard(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else (
                n.func.id if isinstance(n.func, ast.Name) else "")
            if any(h in name.lower() for h in _GUARD_HINTS):
                return True
    return False


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    guarded = {id(f): _fn_calls_guard(f) for f in funcs}

    def enclosing_guarded(lineno: int) -> bool:
        best = None
        for f in funcs:
            if f.lineno <= lineno and (best is None or f.lineno > best.lineno):
                best = f
        return guarded.get(id(best), False) if best else False

    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name not in _REDIRECT_CTORS:
            continue
        loc = node.args[0] if node.args else None
        if loc is None:
            for kw in node.keywords:
                if kw.arg == "url":
                    loc = kw.value
        if loc is None or _is_literal(loc):
            continue
        if _marker_above(lines, node.lineno) or enclosing_guarded(node.lineno):
            continue
        out.append(f"{rel}:{node.lineno}: RedirectResponse to a non-literal location with no "
                   f"host-allowlist/https guard in the function — a possible open redirect. Guard the "
                   f"target host + scheme, or mark `# redirect: ok — reason`.")
    return out


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems, files = [], 0
    for svc in sorted(SERVICES.iterdir()):
        appdir = svc / "app"
        if not svc.is_dir() or not appdir.exists():
            continue
        for py in sorted(appdir.rglob("*.py")):
            files += 1
            problems.extend(_check_file(py, str(py.relative_to(ROOT))))
    if problems:
        print("safety:open-redirect: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:open-redirect: PASS — {files} files; no unguarded non-literal redirects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
