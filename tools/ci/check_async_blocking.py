#!/usr/bin/env python
"""Gate: no blocking I/O primitive runs directly on the async event loop.

THE FOOTGUN THIS EXISTS FOR.

A single-worker async service that calls a BLOCKING primitive (urllib.request.urlopen, requests.*,
time.sleep, a raw socket) directly inside an `async def` freezes the WHOLE worker — every other
request, and the liveness probe — for the call's full timeout whenever the remote is slow or hung.
This already shipped (v0.18.3: an HTTP audit sink + notifier froze /healthz), and the sensitive-
services audit found four more (zatca signer/gateway, connector provider, verification provider,
webhook delivery). The fix is one line — `await asyncio.to_thread(...)` — and the primitive belongs
off the loop.

So: for every `async def`, a direct blocking-primitive CALL that is not moved off the loop is flagged.
A call inside a NESTED plain `def` (the usual to_thread target) or passed BY NAME to
`asyncio.to_thread(fn, ...)` is fine and not flagged.

PLUG-AND-PLAY: the blocking-primitive patterns are config (a repo using httpx-sync or pymysql adds
them); reviewed exceptions are named, never guessed.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# A CALL whose func source matches one of these is a blocking primitive.
_BLOCKING_RE = re.compile(os.environ.get(
    "ASYNC_BLOCKING_RE",
    r"(^|\.)urlopen$|(^|\.)requests\.(get|post|put|delete|patch|head|request)$|"
    r"^requests\.(get|post|put|delete|patch|head|request)$|(^|\.)time\.sleep$|^sleep$"))
_EXEMPT = {x.strip() for x in os.environ.get("ASYNC_BLOCKING_EXEMPT", "").split(",") if x.strip()}


def _func_seg(call: ast.Call, src: str) -> str:
    return (ast.get_source_segment(src, call.func) or "").strip()


def scan_service(svc: pathlib.Path) -> list[str]:
    problems: list[str] = []
    app = svc / "app"
    if not app.is_dir():
        return problems
    for path in sorted(app.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            # nested functions get their own scope — a sync closure is the normal to_thread target,
            # so a blocking call inside it is fine.
            nested = {id(n) for node in fn.body for n in ast.walk(node)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            nested_bodies = set()
            for node in ast.walk(fn):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
                    for sub in ast.walk(node):
                        nested_bodies.add(id(sub))
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and id(node) not in nested_bodies):
                    continue
                seg = _func_seg(node, src)
                if not seg or not _BLOCKING_RE.search(seg):
                    continue
                if f"{svc.name}:{fn.name}" in _EXEMPT:
                    continue
                problems.append(
                    f"{svc.name}: {path.relative_to(svc).as_posix()}:{node.lineno} async {fn.name}() "
                    f"calls blocking {seg}(...) directly on the event loop — wrap it in "
                    f"await asyncio.to_thread(...).")
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
        print(f"\nasync-blocking: FAIL — {len(problems)} blocking call(s) on the event loop")
        return 1
    print("async-blocking: PASS — no blocking primitive runs directly on the event loop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
