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
  3. INDIRECT an `async def` that CALLS a sync `def` whose OWN body blocks directly — the freeze one
              function away, which #1/#2 miss. This is the class that hid a blocking urlopen behind
              post_json() on billing's money path (v0.19.14) and behind self._call()/self._post() in
              onboarding + ticket (v0.19.15). Both a bare-name call to a module function (post_json())
              and a method call (self._post()) count.

              SCOPE-AWARE, NO BLOCKLIST. A name counts as blocking for an async caller ONLY when it
              actually resolves, in THAT file, to a sync def that blocks: a same-file def/method, or a
              name imported `from <module> import name` from a file whose same-named def blocks. This
              removes cross-file name collisions BY CONSTRUCTION — the earlier global registry needed a
              blocklist of generic names ({get,send,read,call,post,…}) to suppress collisions, and that
              blocklist went blind to real bugs wherever those names were genuinely blocking (the fix a
              peer, smart-booking-scheduling, proved on their fleet). Per-file resolution needs no
              blocklist and sees them all.

A call inside a NESTED plain `def`/`lambda` (the usual to_thread target) or passed BY NAME to
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


def _dotted(svc: pathlib.Path, path: pathlib.Path) -> str:
    """app-relative dotted module name, e.g. app.adapters.billing_http."""
    return path.relative_to(svc).with_suffix("").as_posix().replace("/", ".")


def _def_blocks(node: ast.FunctionDef, src: str) -> bool:
    """Does this sync def's OWN body call a blocking/heavy primitive (not in a nested scope)?"""
    nb = _nested_body_ids(node)
    for c in ast.walk(node):
        if isinstance(c, ast.Call) and id(c) not in nb:
            s = _func_seg(c, src)
            if s and (_BLOCKING_RE.search(s) or _HEAVY_RE.search(s)):
                return True
    return False


def _module_blocking_defs(services: list[pathlib.Path]) -> dict[tuple[str, str], set[str]]:
    """(service, dotted-module) -> names of sync defs in that file whose OWN body blocks.

    Includes module functions AND class methods (both matter: post_json() is a module func,
    self._post() is a method). Keyed per module so INDIRECT resolution is scope-aware, never global."""
    out: dict[tuple[str, str], set[str]] = {}
    for svc in services:
        for path, src, tree in _iter_py(svc):
            names = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and _def_blocks(n, src)}
            out[(svc.name, _dotted(svc, path))] = names
    return out


def _visible_blocking(svc: str, cur_mod: str, tree: ast.AST, src: str,
                      mod_defs: dict[tuple[str, str], set[str]]) -> tuple[set[str], set[str]]:
    """The blocking names actually resolvable in THIS file: (bare-callables, self-methods).

    bare = same-file sync defs that block + names imported `from <mod> import name` where <mod> (this
    service) has a same-named blocking def. self-methods = same-file methods that block. No blocklist:
    a name counts only when it truly resolves here, so a same-named def in an unrelated file cannot."""
    same_file = mod_defs.get((svc, cur_mod), set())
    bare = set(same_file)
    methods = set(same_file)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                     # relative import → resolve against this module
                parts = cur_mod.split(".")[:-node.level]
                base = ".".join(parts + ([node.module] if node.module else []))
            src_names = mod_defs.get((svc, base), set())
            for alias in node.names:
                if alias.name in src_names:
                    bare.add(alias.asname or alias.name)
    return bare, methods


def scan_service(svc: pathlib.Path,
                 mod_defs: dict[tuple[str, str], set[str]] | None = None) -> list[str]:
    """DIRECT + HEAVY always; INDIRECT when mod_defs (the per-module blocking-def map) is supplied."""
    problems: list[str] = []
    defs = mod_defs if mod_defs is not None else _module_blocking_defs([svc])
    for path, src, tree in _iter_py(svc):
        bare, methods = _visible_blocking(svc.name, _dotted(svc, path), tree, src, defs)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            nb = _nested_body_ids(fn)   # a blocking call in a nested def/lambda is the to_thread target
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
                elif ("." not in seg and seg in bare) or \
                     (seg.startswith("self.") and seg.count(".") == 1 and seg[5:] in methods):
                    # INDIRECT: a call that resolves, in this file, to a sync def that blocks.
                    target = seg if "." not in seg else seg[5:]
                    problems.append(f"{loc} calls {seg}(...), a sync function that blocks on the event "
                                    f"loop — wrap it in await asyncio.to_thread({target}, ...).")
    return problems


def main() -> int:
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "app").is_dir())
    mod_defs = _module_blocking_defs(services)
    problems: list[str] = []
    for svc in services:
        problems.extend(scan_service(svc, mod_defs))
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\nasync-blocking: FAIL — {len(problems)} blocking call(s) on the event loop")
        return 1
    tracked = sum(len(v) for v in mod_defs.values())
    print("async-blocking: PASS — no blocking primitive runs directly on the event loop "
          f"(direct + heavy + scope-aware indirect; {tracked} sync-blocking helpers tracked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
