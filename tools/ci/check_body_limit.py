#!/usr/bin/env python3
"""safety:body-limit — the body-size cap must not be bypassable by a chunked request.

THE FAILURE THIS EXISTS TO PREVENT (fleet audit 2026-09-12, authz-service-03).

BodyLimitMiddleware used to be a Starlette BaseHTTPMiddleware that only inspected the Content-Length
header. A request sent with Transfer-Encoding: chunked carries NO Content-Length, so the header check
saw nothing and the body streamed straight through uncapped (CWE-400 resource abuse). The whole fleet
shared the identical vulnerable class.

The fix is a PURE-ASGI middleware that counts the streamed bytes and rejects once the cap is crossed,
then replays the buffered body downstream (a swapped receive inside BaseHTTPMiddleware never reaches
the wrapped app via call_next, so BaseHTTPMiddleware cannot do this — the class must be pure ASGI).

This gate enforces, for every service's app/middleware/security.py:
  1. class BodyLimitMiddleware is NOT a BaseHTTPMiddleware subclass (pure ASGI), and
  2. it defines an async __call__(self, scope, receive, send), and
  3. its body handles a no-Content-Length stream (references more_body) — i.e. it counts, not just
     reads a header.

Read as source via ast — a gate that imports and executes what it audits is not a gate.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("BODY_LIMIT_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_CLASS = "BodyLimitMiddleware"


def _check(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == _CLASS), None)
    if cls is None:
        return [f"{_CLASS} not found"]
    problems: list[str] = []
    base_names = {ast.unparse(b) for b in cls.bases}
    if any("BaseHTTPMiddleware" in b for b in base_names):
        problems.append(
            f"{_CLASS} is a BaseHTTPMiddleware subclass — it can only see Content-Length, so a "
            "chunked body bypasses the cap. It must be a pure-ASGI middleware.")
    call = next((n for n in cls.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "__call__"), None)
    if call is None:
        problems.append(f"{_CLASS} has no async __call__(self, scope, receive, send) — not pure ASGI.")
    else:
        args = [a.arg for a in call.args.args]
        if args[:4] != ["self", "scope", "receive", "send"]:
            problems.append(
                f"{_CLASS}.__call__ signature is {args} — expected (self, scope, receive, send).")
    if "more_body" not in ast.unparse(cls):
        problems.append(
            f"{_CLASS} never references `more_body` — it does not count a streamed/chunked body, "
            "so a request without Content-Length is not capped.")
    return problems


def main() -> int:
    problems: list[str] = []
    checked = 0
    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        sec = service / "app" / "middleware" / "security.py"
        if not sec.is_file():
            continue
        checked += 1
        for issue in _check(sec):
            problems.append(f"  {service.name}: {issue}")
    if problems:
        print("      the body-size limit is bypassable by a chunked body in some service(s):")
        print("\n".join(problems))
        return 1
    print(f"      {checked} services cap the body size for a chunked body too (pure-ASGI, byte-counted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
