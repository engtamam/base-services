#!/usr/bin/env python
"""Gate: a sequential `await` fan-out is a latency bug, not a style choice.

`for x in items: r = await fetch(x); out.append(...)` waits for each call to finish before the
next STARTS, so the total wait is the SUM of every call. When the items are independent I/O — the
classic fan-out — that should be `asyncio.gather`, whose wait is the SLOWEST single call. This
exact bug made a login hang for over a minute: the admin/app catalog awaited 12+ services one
after another (fixed v0.24.5).

The tell this gate keys on is the FAN-OUT-COLLECT shape, which is almost never legitimately serial:
a loop (or comprehension) that BOTH awaits a call AND accumulates results into a container.

  1. A `for` / `async for` loop whose body awaits a call AND appends/extends/adds/`+=` into a
     collection. → gather the calls, then build the list.
  2. A comprehension (`[... for ...]`, set/dict/generator) that contains an `await`. An async
     comprehension awaits each element serially too.

An ordered, genuinely-serial loop (each step needs the previous one's result; a rate-limited
drain; writes that must land in order) is legitimate — mark it `# serial:` with a reason, on the
loop line or a comment line just above it, exactly like scan-discipline's `# sweeper:`. Intent
that is not written down does not survive the next reader.

Scanned over each service's `app/core/` and `app/api/` — where request handling and fan-outs live.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

_SERIAL = re.compile(r"#\s*serial:", re.IGNORECASE)

# Accumulation signals — a loop that COLLECTS results (the fan-out-collect shape).
_ACCUMULATORS = frozenset({"append", "extend", "add", "update"})
# Awaits that are never a fan-out to parallelize — a pace/handoff, not an independent I/O call.
_NEVER_FANOUT = frozenset({"sleep", "acquire", "release", "wait", "join", "gather", "wait_for"})
# The gate targets a fan-out to INDEPENDENT SERVICES (network), not a loop over a LOCAL port. A
# datastore/cache/queue op in a loop (backup restore, erasure, a ledger write, a CAS retry) is a
# different concern — often legitimately serial for ordering/backpressure — and gathering DB writes is
# a separate judgement call, not this gate's. So an await whose call RECEIVER is a local port is not a
# fan-out candidate; a call to an injected fetcher (a bare name) or a client/adapter/peer IS.
_LOCAL_PORT_RECEIVERS = frozenset({
    "store", "_store", "_s", "datastore", "_datastore", "_ds", "cache", "_cache", "queue", "_queue",
    "event_bus", "_event_bus", "bus", "_bus", "rate_limiter", "_rl", "secrets", "_secrets",
    "_jobs", "jobs", "_sessions", "_schedules", "_targets", "_coupons", "_subs", "_invoices",
    "_payments", "_ledger", "_usage", "_settlements", "_idempo", "_deliveries", "_prefs", "_inapp",
    "_templates", "_wa_templates", "_seen", "_outbox",
})
# Local store-verb methods — even on `self`/an aliased repo, these are a datastore op, not a service call.
_STORE_VERBS = frozenset({
    "get", "put", "delete", "save", "fetch", "find", "scan_all", "list", "cas", "add_fields",
    "incr", "incr_if_below", "index_due", "unindex_due", "index_leased", "unindex_leased",
    "unique_get", "unique_reserve", "unique_clear", "count", "counts", "remember", "seen",
})
# Statement types we recurse THROUGH inside a loop body (but never into a nested scope/loop, which
# is checked as its own node).
_TRANSPARENT = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.While, ast.ExceptHandler)
_OPAQUE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.For, ast.AsyncFor,
           ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _serial_justified(lines: list[str], lineno: int) -> bool:
    """A `# serial:` note on the loop line, or on any comment line directly above it."""
    if lineno - 1 < len(lines) and _SERIAL.search(lines[lineno - 1]):
        return True
    i = lineno - 2
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if not stripped.startswith("#"):
            return False
        if _SERIAL.search(stripped):
            return True
        i -= 1
    return False


def _is_fanout_await(node: ast.Await) -> bool:
    """True when `await <call>(...)` looks like a call to an INDEPENDENT service (a fan-out
    candidate), False when it is a local-port op (datastore/cache/queue) or a pace/handoff.

    - bare-name call `fetch(t)` / `fetch_manifest(t)`  → candidate (the injected-fetcher pattern).
    - attribute call on a local-port receiver (`self._store.get`) → NOT a candidate.
    - attribute call whose method is a store verb (`.get`/`.put`/…) → NOT a candidate.
    - attribute call whose method is a pace/handoff (`.sleep`/`.acquire`/…) → NOT a candidate.
    - any other attribute call (`client.get`, `self._gateway.charge`, `self._svc.reserve`) → candidate.
    """
    val = node.value
    if not isinstance(val, ast.Call):
        return False
    f = val.func
    if isinstance(f, ast.Name):
        return f.id not in _NEVER_FANOUT
    if isinstance(f, ast.Attribute):
        if f.attr in _NEVER_FANOUT or f.attr in _STORE_VERBS:
            return False
        recv = f.value
        recv_name = recv.attr if isinstance(recv, ast.Attribute) else getattr(recv, "id", "")
        if recv_name in _LOCAL_PORT_RECEIVERS:
            return False
        return True
    return False


def _body_stmts(stmts: list[ast.stmt]):
    """Yield statements in a loop body, recursing through if/try/with/while but NOT into a nested
    loop, comprehension, or function — each of those is analysed as its own node."""
    for s in stmts:
        yield s
        for child in ast.iter_child_nodes(s):
            if isinstance(child, _OPAQUE):
                continue
            if isinstance(child, _TRANSPARENT):
                yield from _body_stmts(getattr(child, "body", []) or [])
                yield from _body_stmts(getattr(child, "orelse", []) or [])
                yield from _body_stmts(getattr(child, "finalbody", []) or [])
                yield from _body_stmts(getattr(child, "handlers", []) or [])


def _shallow_nodes(stmt: ast.stmt):
    """Every node inside a single statement, not descending into a nested scope/loop/comprehension."""
    stack = [stmt]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if isinstance(c, _OPAQUE):
                continue
            stack.append(c)


def _loop_has_fanout_collect(loop) -> bool:
    stmts = list(_body_stmts(loop.body))
    has_await_call = False
    has_accumulate = False
    for s in stmts:
        for n in _shallow_nodes(s):
            if isinstance(n, ast.Await):
                if _is_fanout_await(n):
                    has_await_call = True
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in _ACCUMULATORS:
                has_accumulate = True
            elif isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add):
                has_accumulate = True
        if has_await_call and has_accumulate:
            return True
    return False


def _comp_has_await(node) -> bool:
    """A comprehension whose element or a nested clause awaits — awaited serially per element."""
    for child in ast.walk(node):
        if isinstance(child, ast.Await) and _is_fanout_await(child):
            return True
    return False


def check_file(path: pathlib.Path) -> list[str]:
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()                       # a file outside the repo (e.g. a test tmp file)
    return check_source(path.read_text(encoding="utf-8"), rel)


def check_source(text: str, rel: str) -> list[str]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel}: unparseable: {exc}"]
    problems: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if _loop_has_fanout_collect(node) and not _serial_justified(lines, node.lineno):
                problems.append(
                    f"{rel}:{node.lineno}: sequential `await` in a fan-out loop (awaits a call and "
                    f"collects results). Use asyncio.gather so the wait is the slowest call, not the "
                    f"sum. If the loop must be serial (ordered/dependent), add `# serial:` with why.")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if _comp_has_await(node) and not _serial_justified(lines, node.lineno):
                problems.append(
                    f"{rel}:{node.lineno}: `await` inside a comprehension runs serially per element. "
                    f"Build the awaitables and asyncio.gather them. If intentional, add `# serial:`.")
    return problems


def main() -> int:
    targets = sorted(
        p for svc in SERVICES.iterdir() if svc.is_dir()
        for sub in ("core", "api")
        if (svc / "app" / sub).is_dir()
        for p in (svc / "app" / sub).rglob("*.py")
    )
    if not targets:
        print("sequential-fanout: FAIL - no app/core or app/api found; the gate sees nothing")
        return 1
    problems = [p for t in targets for p in check_file(t)]
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"\nsequential-fanout: FAIL - {len(problems)} problem(s) in {len(targets)} files")
        return 1
    print(f"sequential-fanout: PASS - {len(targets)} files; "
          f"no unjustified sequential await fan-out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
