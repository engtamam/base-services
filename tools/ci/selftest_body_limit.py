#!/usr/bin/env python
"""Mutation proof for safety:body-limit. A gate that cannot fail is decoration.

Writes a CORRECT pure-ASGI BodyLimitMiddleware, confirms it passes, then breaks it one way at a time
and demands the gate go red each time.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_body_limit.py"

GOOD = '''
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if len(body) > self._max:
                await JSONResponse({"error": "PAYLOAD_TOO_LARGE"}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        async def _replay():
            return {"type": "http.request", "body": body, "more_body": False}
        return await self.app(scope, _replay, send)
'''

MUTATIONS = {
    "M1 back to BaseHTTPMiddleware": GOOD.replace(
        "class BodyLimitMiddleware:",
        "from starlette.middleware.base import BaseHTTPMiddleware\nclass BodyLimitMiddleware(BaseHTTPMiddleware):"),
    "M2 no more_body counting (header-only)": GOOD.replace("more_body", "xxx_body"),
    "M3 not async __call__ / wrong signature": GOOD.replace(
        "async def __call__(self, scope, receive, send):",
        "async def __call__(self, request, call_next):"),
}


def write(base: pathlib.Path, src: str) -> None:
    mw = base / "services" / "x-service" / "app" / "middleware"
    mw.mkdir(parents=True, exist_ok=True)
    (mw / "security.py").write_text(src, encoding="utf-8")


def run_gate(base: pathlib.Path) -> int:
    return subprocess.run([sys.executable, str(CHECK)],
                          env={**os.environ, "BODY_LIMIT_GATE_ROOT": str(base)},
                          capture_output=True, text=True).returncode


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        write(base, GOOD)
        if run_gate(base) != 0:
            print("  FAIL: the gate rejects a CORRECT pure-ASGI middleware"); return 1
        print("  ok   correct pure-ASGI middleware passes")

        for name, mutated in MUTATIONS.items():
            write(base, mutated)
            if run_gate(base) == 0:
                failures.append(name); print(f"  FAIL {name} — SURVIVED")
            else:
                print(f"  ok   {name} — caught")

    if failures:
        print(f"\nsafety:body-limit selftest FAILED: {len(failures)} mutation(s) survived.")
        return 1
    print("\nsafety:body-limit selftest OK — every mutation caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
