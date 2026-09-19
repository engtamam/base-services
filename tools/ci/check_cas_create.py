#!/usr/bin/env python3
"""safety:cas-create — a row that is version-pinned cas()-updated must be CREATED via cas, not put().

THE FAILURE THIS EXISTS TO PREVENT (identity refresh-rotation incident 2026-09-19; payments audit C1).

A row created with `put()` carries no `version`. On the postgres datastore a version-pinned
`cas(coll, key, ver, …)` UPDATEs `WHERE (value->>'version')::bigint = ver`, which never matches a
put()-written row — so the update no-ops forever (in identity this 401'd EVERY refresh in production
while the memory adapter, which tolerated the missing version, kept the tests green). The rule: any
collection that is ever transitioned by a version-pinned cas() must be CREATED by a cas insert-if-
absent (`cas(coll, key, 0, …)`), never by put().

This gate flags, per service `app/core`, a collection C that is:
  • version-pinned cas-updated:  `await <s>.cas(C, k, <ver>, …)` with <ver> NOT the literal 0, and
  • created by put:              `await <s>.put(C, …)` appears for C, and
  • never insert-if-absent:      no `await <s>.cas(C, k, 0, …)` for C.
Collection identity is the source text of the first argument (usually a module constant). Reads via
`ast`. A legitimate exception opts out with an in-line `# cas-create: ok — reason` on the put.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("CAS_CREATE_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"


def _await_attr_call(node):
    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) \
            and isinstance(node.value.func, ast.Attribute):
        return node.value.func.attr, node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr, node
    return None, None


def _coll(call: ast.Call) -> str | None:
    if call.args:
        try:
            return ast.unparse(call.args[0])
        except Exception:  # noqa: BLE001
            return None
    return None


def _has_marker(line: str) -> bool:
    return "# cas-create: ok" in line


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    cas_updated: set[str] = set()          # collections with a version-pinned cas (ver != literal 0)
    cas_created: set[str] = set()          # collections with cas insert-if-absent (ver == 0)
    puts: dict[str, int] = {}              # collection -> first put lineno
    for node in ast.walk(tree):
        method, call = _await_attr_call(node)
        if method == "cas" and len(call.args) >= 3:
            c = _coll(call)
            if c is None:
                continue
            ver = call.args[2]
            if isinstance(ver, ast.Constant) and ver.value == 0:
                cas_created.add(c)
            else:
                cas_updated.add(c)
        elif method == "put":
            c = _coll(call)
            if c is not None and c not in puts:
                puts[c] = node.lineno if isinstance(node, ast.Await) else call.lineno
    out: list[str] = []
    for c in sorted(cas_updated):
        if c in puts and c not in cas_created:
            lineno = puts[c]
            line = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
            if _has_marker(line):
                continue
            out.append(f"{rel}:{lineno}: collection {c} is version-pinned cas()-updated but CREATED "
                       f"via put() (no cas insert-if-absent) — on postgres the update never matches "
                       f"(the identity refresh-rotation bug). Create it with cas(C, k, 0, …), or mark "
                       f"`# cas-create: ok — reason`.")
    return out


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems, checked = [], 0
    for svc in sorted(SERVICES.iterdir()):
        core = svc / "app" / "core"
        if not svc.is_dir() or not core.exists():
            continue
        checked += 1
        for py in sorted(core.rglob("*.py")):
            problems.extend(_check_file(py, str(py.relative_to(ROOT))))
    if problems:
        print("safety:cas-create: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:cas-create: PASS — {checked} services; every cas-updated collection is cas-created")
    return 0


if __name__ == "__main__":
    sys.exit(main())
