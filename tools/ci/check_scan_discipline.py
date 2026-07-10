#!/usr/bin/env python
"""Gate: a full-collection scan on a request path is a bug, not a slow query.

Three rules, checked over every service's `app/core/` — the layer where request handling lives.

1. **No unexplained `scan_all()`.** A sweeper and a forgotten request path look identical from
   outside; the only difference is intent, and intent that is not written down does not survive
   the next reader. Every `scan_all()` call must carry a `# sweeper:` comment (on the call, or
   on a line just above it) saying why reading every row IS the operation.

2. **No blanket scan constants.** `1_000_000` was never a limit anyone chose; it was a number
   large enough to look like "all". `LIMIT` without `ORDER BY` does not merely truncate — it
   returns an arbitrary subset. A tenant could see none of its own rows and nothing errors.
   Any integer literal above `_MAX_BOUND` in a scan-shaped call is refused.

3. **`find()` must be scoped.** `find(collection, {})` is a cross-tenant leak one typo away
   from a repository that meant to filter. The adapters raise on it at runtime; this catches
   the literal at review time, which is cheaper.

Why a gate and not a code review: this bug reappears every time someone needs "just the rows
for this tenant" and the store has no index. It reappeared in seventeen services at once.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# Above this, a limit is not a page — it is "everything", spelled defensively.
_MAX_BOUND = 100_000

_SWEEPER = re.compile(r"#\s*sweeper:", re.IGNORECASE)


def _justified(lines: list[str], lineno: int) -> bool:
    """A `# sweeper:` note on the call, or on any comment line directly above it."""
    if _SWEEPER.search(lines[lineno - 1]):
        return True
    i = lineno - 2
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if not stripped.startswith("#"):
            return False
        if _SWEEPER.search(stripped):
            return True
        i -= 1
    return False


def _attr_name(node: ast.AST) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def check_file(path: pathlib.Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:                       # a broken file is someone else's gate
        return [f"{path}: unparseable: {exc}"]

    rel = path.relative_to(ROOT).as_posix()
    problems: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_name(node.func)

        if name == "scan_all":
            if not _justified(lines, node.lineno):
                problems.append(
                    f"{rel}:{node.lineno}: scan_all() with no `# sweeper:` justification. "
                    f"If this is a request path, use find(). If it is a cron sweeper, say so."
                )
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int) \
                        and arg.value > _MAX_BOUND:
                    problems.append(
                        f"{rel}:{node.lineno}: scan cap {arg.value:,} is a blanket 'everything'. "
                        f"Bound it below {_MAX_BOUND:,} and page."
                    )

        if name == "find":
            for kw in node.keywords:
                if kw.arg is None:
                    continue
            if len(node.args) >= 2:
                where = node.args[1]
                if isinstance(where, ast.Dict) and not where.keys:
                    problems.append(
                        f"{rel}:{node.lineno}: find() with an empty filter reads every tenant."
                    )

    # The constant that started this. Checked against CODE, not prose: the comment that
    # explains why it is gone is allowed to name it.
    code = "\n".join(line.split("#", 1)[0] for line in lines)
    code = re.sub(r'("""|\'\'\')(.|\n)*?\1', "", code)
    for literal in ("1_000_000", "1000000"):
        if literal in code:
            problems.append(f"{rel}: the million-row scan constant is back ({literal}).")
    return problems


def main() -> int:
    targets = sorted(
        p for svc in SERVICES.iterdir() if svc.is_dir()
        for p in (svc / "app" / "core").rglob("*.py") if (svc / "app" / "core").is_dir()
    )
    if not targets:
        print("scan-discipline: FAIL - no app/core/ found; the gate is looking at nothing")
        return 1

    problems = [p for t in targets for p in check_file(t)]
    for problem in problems:
        print(f"  {problem}")

    scanned = len(targets)
    if problems:
        print(f"\nscan-discipline: FAIL - {len(problems)} problem(s) in {scanned} core files")
        return 1
    print(f"scan-discipline: PASS - {scanned} core files; "
          f"every scan_all() on a request path is justified or gone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
