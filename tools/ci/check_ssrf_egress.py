#!/usr/bin/env python3
"""safety:ssrf-egress — an outbound HTTP fetch to a NON-LITERAL URL needs an egress guard.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding H1).

`urllib.request.urlopen(url)` / `requests.get(url)` / `httpx.*(url)` where `url` is built from config or
a stored/tenant credential (e.g. MERCHANT_API_BASE) is an SSRF sink: a malicious base reaches cloud
metadata (169.254.169.254), loopback, private ranges, or file://. Fetches to a hardcoded literal URL
are fine; the risk is a variable URL with no scheme/IP-range guard.

This gate flags an HTTP-client call whose URL argument is NOT a string literal and whose enclosing
function calls NO guard (a function whose name contains guard/validate/ssrf/allowlist/safe_url). It is
ADVISORY/heuristic — a fetch to a trusted fixed-config host opts out with an in-line
`# ssrf: ok — reason`. Reads source via `ast`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("SSRF_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "request", "send", "stream"}
_URLOPEN = {"urlopen"}
_GUARD_HINTS = ("guard", "validate_url", "validate_gateway", "ssrf", "allowlist", "safe_url", "_send")


def _has_marker(line: str) -> bool:
    return "# ssrf: ok" in line


def _marker_above(lines: list[str], lineno: int) -> bool:
    for i in range(lineno - 1, max(-1, lineno - 4), -1):
        if 0 <= i < len(lines) and _has_marker(lines[i]):
            return True
    return False


def _first_url_arg(call: ast.Call):
    return call.args[0] if call.args else None


def _is_literal(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


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
    # map each function to whether it calls a guard
    out: list[str] = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    guarded = {id(f): _fn_calls_guard(f) for f in funcs}

    def enclosing_guarded(lineno: int) -> bool:
        best = None
        for f in funcs:
            if f.lineno <= lineno and (best is None or f.lineno > best.lineno):
                if lineno <= (max((c.lineno for c in ast.walk(f)), default=f.lineno)):
                    best = f
        return guarded.get(id(best), False) if best else False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        is_urlopen = method in _URLOPEN
        is_http = method in _HTTP_METHODS and any(
            h in ast.unparse(node.func.value).lower() for h in ("request", "http", "client", "session", "requests", "httpx", "urllib"))
        if not (is_urlopen or is_http):
            continue
        url = _first_url_arg(node)
        if url is None or _is_literal(url):
            continue
        if _marker_above(lines, node.lineno) or enclosing_guarded(node.lineno):
            continue
        out.append(f"{rel}:{node.lineno}: {method}() to a non-literal URL with no egress guard — an "
                   f"SSRF sink if the URL comes from config/a tenant credential. Add a scheme+IP-range "
                   f"guard, or mark `# ssrf: ok — reason`.")
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
        print("safety:ssrf-egress: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:ssrf-egress: PASS — {files} files; no unguarded non-literal HTTP fetches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
