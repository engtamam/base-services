#!/usr/bin/env python
"""Advisory scanner — CANDIDATES for optimisation across services/*/app, from DB access to Python.

NOT a gate and NOT a claim of a win: it reports SITES that MATCH a pattern research + profiling flagged
as sometimes-costly (docs/performance/binary-operations.md). A PROFILER decides what actually pays —
optimising a site this prints without measuring it first is guessing. Profiling billing's hottest path
(EntitlementResolver.resolve) showed the real cost was copy.deepcopy + a per-key merge loop, NOT
anything bitwise — hence this scans the real perf anti-patterns too, not just bit tricks. AST-based
(not regex); test files and non-/app/ dirs are skipped.

Bitwise / set-structure family (small, situational wins):
  A power2-arith   x * / // % a power-of-two CONSTANT  -> shift/mask (x*8 -> x<<3, x%16 -> x&15)
  B closed-set     `x in (lit, ...)` of all-constant literals -> module-level frozenset (or a bitmask
                   if the universe is closed)
  C set-algebra    set(...) & | -  or .intersection/.union/... -> Roaring/Bloom if the sets are LARGE

Real perf anti-patterns (usually the bigger levers — the profiler's actual findings):
  D deepcopy       copy.deepcopy(...) -> a defensive copy on a read/hot path is often the #1 cost;
                   copy shallowly, or don't copy on read
  E seq-io-in-loop `await <...>.get/find/fetch/save/put/emit/...(...)` inside a for/while -> N+1
                   sequential I/O; batch it or asyncio.gather the calls
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
_SETLIKE = {"intersection", "union", "difference", "isdisjoint", "issubset", "issuperset"}
# Method names that almost always mean an I/O round-trip (datastore / cache / bus / gateway).
_IO_METHODS = {"get", "find", "fetch", "save", "put", "delete", "scan_all", "incr", "cas",
               "emit", "publish", "charge", "claim", "mark", "aggregate", "consume"}


def _is_pow2(n: object) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n > 1 and (n & (n - 1)) == 0


def _const(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) else None


def scan(path: pathlib.Path, src: str) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits: list[tuple[int, str, str]] = []

    # E: an awaited I/O-shaped call anywhere inside a for/while (N+1 sequential I/O). Deduped by node
    # identity so a call nested in several loops is reported ONCE, not per enclosing loop.
    seen_await: dict[int, ast.Await] = {}
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.While)):
            continue
        for n in ast.walk(loop):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and \
                    isinstance(n.value.func, ast.Attribute) and n.value.func.attr in _IO_METHODS:
                seen_await[id(n)] = n
    for n in seen_await.values():
        hits.append((n.lineno, "seq-io-in-loop",
                     f"await .{n.value.func.attr}(...) inside a loop -> batch / asyncio.gather"))

    for node in ast.walk(tree):
        # D: a deepcopy (the profiler's #1 cost on the resolve hot path)
        if isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "deepcopy") or \
               (isinstance(fn, ast.Name) and fn.id == "deepcopy"):
                hits.append((node.lineno, "deepcopy",
                             "copy.deepcopy(...) -> avoid on a read/hot path; shallow-copy or don't copy"))
        # A: arithmetic by a power-of-two constant
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Mod, ast.FloorDiv, ast.Div)):
            for operand in (node.right, node.left):
                if _is_pow2(_const(operand)):
                    opname = type(node.op).__name__
                    hits.append((node.lineno, "power2-arith",
                                 f"{opname} by power-of-two {_const(operand)} -> shift/mask"))
                    break
        # B: membership against an all-literal tuple/list/set
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and \
                isinstance(node.ops[0], (ast.In, ast.NotIn)):
            rhs = node.comparators[0]
            if isinstance(rhs, (ast.Tuple, ast.List, ast.Set)) and rhs.elts and \
                    all(isinstance(e, ast.Constant) for e in rhs.elts):
                hits.append((node.lineno, "closed-set",
                             f"`in` over {len(rhs.elts)} literals -> frozenset / bitmask"))
        # C: set algebra
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr in _SETLIKE:
            hits.append((node.lineno, "set-algebra",
                         f".{node.func.attr}() -> Roaring/Bloom if the sets are large"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr, ast.Sub)):
            # only when at least one side is an explicit set literal / set(...) call (avoid int math)
            def _setish(n: ast.AST) -> bool:
                return isinstance(n, ast.Set) or (isinstance(n, ast.Call)
                                                  and isinstance(n.func, ast.Name) and n.func.id == "set")
            if _setish(node.left) or _setish(node.right):
                hits.append((node.lineno, "set-algebra",
                             f"{type(node.op).__name__} on sets -> Roaring/Bloom if large"))
    return hits


def main() -> int:
    counts: dict[str, int] = {}
    total = 0
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template"):
        app = svc / "app"
        if not app.is_dir():
            continue
        for path in sorted(app.rglob("*.py")):
            try:
                hits = scan(path, path.read_text(encoding="utf-8"))
            except OSError:
                continue
            for lineno, cat, msg in hits:
                counts[cat] = counts.get(cat, 0) + 1
                total += 1
                print(f"  {svc.name}: {path.relative_to(svc).as_posix()}:{lineno} [{cat}] {msg}")
    print(f"\n{total} candidate(s): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("Advisory only — PROFILE before optimising any of these. See docs/performance/binary-operations.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
