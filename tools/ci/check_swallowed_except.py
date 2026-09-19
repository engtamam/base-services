#!/usr/bin/env python3
"""safety:swallowed-except — a broad except that hides its error must log (or re-raise).

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding M8).

`except Exception: return {...}` / `: pass` with no log turns a real bug into a silent wrong answer —
the webhook that "succeeded" while doing nothing, the defensive catch that masks a crash. A broad
catch is sometimes right (best-effort, anti-retry-storm), but then it must SAY it: log before
swallowing, or carry the repo's deliberate-broad marker `# noqa: BLE001`.

This gate flags an `except Exception|BaseException|bare:` handler whose body neither logs (a call whose
name contains log/warning/error/exception/critical/capture/record) nor raises, AND whose `except` line
carries no `# noqa: BLE001` and no `# swallow: ok`. Reads source via `ast`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("SWALLOW_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_LOG_HINTS = ("log", "warning", "warn", "error", "exception", "critical", "capture", "record", "print")


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True                                    # bare except:
    names = []
    if isinstance(t, ast.Name):
        names = [t.id]
    elif isinstance(t, ast.Tuple):
        names = [e.id for e in t.elts if isinstance(e, ast.Name)]
    return any(n in ("Exception", "BaseException") for n in names)


def _body_logs_or_raises(handler: ast.ExceptHandler) -> bool:
    for n in ast.walk(handler):
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else (
                n.func.id if isinstance(n.func, ast.Name) else "")
            if any(h in name.lower() for h in _LOG_HINTS):
                return True
    return False


def _line_ok(line: str) -> bool:
    return "# noqa: BLE001" in line or "# swallow: ok" in line


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        if _body_logs_or_raises(node):
            continue
        line = lines[node.lineno - 1] if 0 <= node.lineno - 1 < len(lines) else ""
        if _line_ok(line):
            continue
        out.append(f"{rel}:{node.lineno}: broad except that neither logs nor re-raises — a silently "
                   f"swallowed error. Log before swallowing, or mark the deliberate catch with "
                   f"`# noqa: BLE001`.")
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
        print("safety:swallowed-except: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:swallowed-except: PASS — {files} files; no unmarked error-swallowing broad excepts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
