#!/usr/bin/env python
"""Gate: no blocking I/O primitive runs directly on the async event loop.

THE FOOTGUN THIS EXISTS FOR.

A single-worker async service that calls a BLOCKING primitive (urllib.request.urlopen, requests.*,
time.sleep, subprocess.*, os.system, a raw socket, a heavy hash) directly inside an `async def` freezes
the WHOLE worker — every other request, and the liveness probe — for the call's full timeout whenever
the remote is slow or hung. This already shipped (v0.18.3: an HTTP audit sink + notifier froze
/healthz), and the sensitive-services audit found four more. The fix is one line —
`await asyncio.to_thread(...)` — and the primitive belongs off the loop.

THREE detections, so an indirection can't hide the freeze:

  1. DIRECT   an `async def` whose own body CALLS a blocking primitive (urlopen/requests/time.sleep/
              subprocess/os.system) not moved off the loop.
  2. HEAVY    an `async def` whose own body CALLS heavy synchronous work — a bare open() (file I/O) or a
              deliberately-slow hash (hashlib.pbkdf2_hmac/scrypt, bcrypt.hashpw/checkpw). These block
              for real time even with no network.
  3. INDIRECT an `async def` that CALLS a plain `def` (by bare name) whose OWN body blocks directly —
              the freeze one function away, which #1/#2 miss. This is the class that hid a blocking
              urlopen behind post_json() on billing's money path (v0.19.14). A blocklist of too-generic
              plain-def names ({get,send,read,call,request,…}) keeps unrelated name collisions from
              false-positiving — NARROW that list, never widen it by guessing.

A call inside a NESTED plain `def` (the usual to_thread target) or passed BY NAME to
`asyncio.to_thread(fn, ...)` / run_in_threadpool is fine and not flagged — for #3, wrapping the call as
`asyncio.to_thread(post_json, ...)` makes post_json an ARGUMENT, not a call, so it is correctly clean.

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

# A CALL whose func source matches one of these is a blocking primitive (DIRECT).
_BLOCKING_RE = re.compile(os.environ.get(
    "ASYNC_BLOCKING_RE",
    r"(^|\.)urlopen$|(^|\.)requests\.(get|post|put|delete|patch|head|request)$|"
    r"^requests\.(get|post|put|delete|patch|head|request)$|(^|\.)time\.sleep$|^sleep$|"
    r"(^|\.)subprocess\.(run|call|check_call|check_output|Popen)$|(^|\.)os\.system$"))
# Heavy SYNCHRONOUS work: file I/O and deliberately-slow hashes. Blocks real time with no network.
_HEAVY_RE = re.compile(os.environ.get(
    "ASYNC_HEAVY_RE",
    r"^open$|(^|\.)hashlib\.pbkdf2_hmac$|(^|\.)hashlib\.scrypt$|(^|\.)bcrypt\.(hashpw|checkpw)$"))
_EXEMPT = {x.strip() for x in os.environ.get("ASYNC_BLOCKING_EXEMPT", "").split(",") if x.strip()}

# Plain-def names too generic to register for INDIRECT detection — an unrelated method sharing the name
# would false-positive. Empirically derived; NARROW it, never widen it by guessing.
_INDIRECT_BLOCKLIST = {"get", "_get", "send", "_send", "read", "_read", "call", "_call",
                       "request", "_request", "__init__"}


def _func_seg(call: ast.Call, src: str) -> str:
    return (ast.get_source_segment(src, call.func) or "").strip()


def _nested_body_ids(fn: ast.AST) -> set[int]:
    """ids of every node that lives inside a nested scope of fn (its own to_thread targets).

    A nested `def`/`async def` AND a `lambda` both count: `asyncio.to_thread(lambda: open(p).read())`
    offloads the open() exactly as a nested def would, so a blocking call inside either is fine."""
    ids: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            for sub in ast.walk(node):
                ids.add(id(sub))
        elif isinstance(node, ast.Lambda):
            for sub in ast.walk(node.body):
                ids.add(id(sub))
    return ids


def _iter_py(svc: pathlib.Path):
    app = svc / "app"
    if not app.is_dir():
        return
    for path in sorted(app.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        yield path, src, tree


def _blocking_plain_defs(services: list[pathlib.Path]) -> set[str]:
    """Pass 1: bare names of plain `def`s whose OWN body directly blocks (DIRECT or HEAVY),
    minus the too-generic blocklist. These are the indirection a #3 async caller hides behind."""
    names: set[str] = set()
    for svc in services:
        for _path, src, tree in _iter_py(svc):
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name in _INDIRECT_BLOCKLIST:
                    continue
                nb = _nested_body_ids(node)
                for c in ast.walk(node):
                    if isinstance(c, ast.Call) and id(c) not in nb:
                        s = _func_seg(c, src)
                        if s and (_BLOCKING_RE.search(s) or _HEAVY_RE.search(s)):
                            names.add(node.name)
                            break
    return names


def scan_service(svc: pathlib.Path, blocking_names: set[str] | None = None) -> list[str]:
    """DIRECT + HEAVY always; INDIRECT when blocking_names (Pass 1's registry) is supplied."""
    problems: list[str] = []
    names = blocking_names or set()
    for path, src, tree in _iter_py(svc):
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            nb = _nested_body_ids(fn)   # a blocking call in a nested def is the normal to_thread target
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and id(node) not in nb):
                    continue
                if f"{svc.name}:{fn.name}" in _EXEMPT:
                    continue
                seg = _func_seg(node, src)
                if not seg:
                    continue
                loc = (f"{svc.name}: {path.relative_to(svc).as_posix()}:{node.lineno} "
                       f"async {fn.name}()")
                if _BLOCKING_RE.search(seg):
                    problems.append(f"{loc} calls blocking {seg}(...) directly on the event loop — "
                                    f"wrap it in await asyncio.to_thread(...).")
                elif _HEAVY_RE.search(seg):
                    problems.append(f"{loc} runs heavy sync {seg}(...) on the event loop — "
                                    f"move it to await asyncio.to_thread(...).")
                elif "." not in seg and seg in names:
                    # INDIRECT: a bare-name call to a plain def that itself blocks (Pass 1 registered it).
                    problems.append(f"{loc} calls {seg}(...), a sync function that blocks on the event "
                                    f"loop — wrap it in await asyncio.to_thread({seg}, ...).")
    return problems


def main() -> int:
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "app").is_dir())
    blocking_names = _blocking_plain_defs(services)
    problems: list[str] = []
    for svc in services:
        problems.extend(scan_service(svc, blocking_names))
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\nasync-blocking: FAIL — {len(problems)} blocking call(s) on the event loop")
        return 1
    print("async-blocking: PASS — no blocking primitive runs directly on the event loop "
          f"(direct + heavy + indirect; {len(blocking_names)} sync-blocking helpers tracked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
