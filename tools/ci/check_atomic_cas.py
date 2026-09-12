#!/usr/bin/env python3
"""safety:atomic-cas — a read-modify-write on the SAME datastore key must use compare-and-set.

THE FAILURE THIS EXISTS TO PREVENT (fleet audit 2026-09-12: ticket-05/06, verification-04/05,
audit-01/06, stream-04, zatca-01).

The recurring class was a `get(coll, key)` … `put(coll, key, …)` in one function with no atomic guard
between them: two concurrent callers both read the same version and both write, so one update is lost
— a breached quota, a forked hash chain, a double-counted anti-abuse row. The datastore exposes
`cas()` / `create()` / `put_if_absent()` exactly to close this; the fix everywhere was to route the
write through one of them.

This gate flags, in each service's app/core/*.py, a function that:
  • reads a key:      `… = await <s>.get(<C>, <K>)`, and later
  • writes that key:  `await <s>.put(<C>, <K>, …)`   (same collection AND key source text)
  • with NO `await <s>.cas(…)` / `.create(…)` / `.put_if_absent(…)` anywhere in that function.

A legitimate single-writer path (no concurrency) opts out with an in-line `# atomic: ok — reason`
on the put, or a function-level one on the `def` line.

Read as source via ast — a gate that imports what it audits is not a gate.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("ATOMIC_CAS_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_ATOMIC = {"cas", "create", "put_if_absent"}


def _await_call(node):
    """If node is `await X.method(...)`, return (method, call). Else (None, None)."""
    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) \
            and isinstance(node.value.func, ast.Attribute):
        return node.value.func.attr, node.value
    return None, None


def _key_sig(call: ast.Call) -> tuple[str, str] | None:
    """(collection_src, key_src) for a get/put call with >=2 positional args, else None."""
    if len(call.args) >= 2:
        try:
            return ast.unparse(call.args[0]), ast.unparse(call.args[1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _has_marker(line: str) -> bool:
    return "# atomic: ok" in line


def _check_func(fn: ast.AST, src_lines: list[str], rel: str) -> list[str]:
    # function-level opt-out on the def line
    def_line = src_lines[fn.lineno - 1] if fn.lineno - 1 < len(src_lines) else ""
    if _has_marker(def_line):
        return []
    gets: set[tuple[str, str]] = set()
    puts: list[tuple[tuple[str, str], int]] = []
    has_atomic = False
    for node in ast.walk(fn):
        method, call = _await_call(node)
        if method is None:
            continue
        if method in _ATOMIC:
            has_atomic = True
        elif method == "get":
            sig = _key_sig(call)
            if sig:
                gets.add(sig)
        elif method == "put":
            sig = _key_sig(call)
            if sig:
                puts.append((sig, node.lineno))
    if has_atomic:
        return []
    out: list[str] = []
    for sig, lineno in puts:
        if sig in gets:
            line = src_lines[lineno - 1] if lineno - 1 < len(src_lines) else ""
            if _has_marker(line):
                continue
            out.append(f"{rel}:{lineno}: read-modify-write on the same key "
                       f"({sig[0]}, {sig[1]}) with no cas()/create()/put_if_absent() — a lost-update "
                       f"race. Use an atomic write, or mark `# atomic: ok — reason` if single-writer.")
    return out


def _check_file(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    src_lines = src.splitlines()
    rel = str(path.relative_to(ROOT))
    tree = ast.parse(src)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            problems.extend(_check_func(node, src_lines, rel))
    return problems


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems: list[str] = []
    checked = 0
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir() or svc.name.startswith("_"):
            continue
        core = svc / "app" / "core"
        if not core.exists():
            continue
        checked += 1
        for py in sorted(core.rglob("*.py")):
            problems.extend(_check_file(py))
    if problems:
        print("safety:atomic-cas: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:atomic-cas: PASS — {checked} services; no unguarded same-key read-modify-write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
