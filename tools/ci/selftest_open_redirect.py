#!/usr/bin/env python3
"""Mutation self-test for safety:open-redirect — a gate that cannot fail is decoration.

Confirms: a RedirectResponse to a non-literal with no guard FAILS; an inline host-allowlist guard
PASSES; a literal path PASSES; and a `# redirect: ok` marker re-exempts.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_open_redirect.py"

_BAD = "def f(url):\n    return RedirectResponse(url)\n"
_GUARDED = ("def f(url, parts, allowed):\n"
            "    if parts.hostname not in allowed:\n"
            "        raise Exception('no')\n"
            "    return RedirectResponse(url)\n")
_LITERAL = "def f():\n    return RedirectResponse('/login')\n"
_MARKED = "def f(url):\n    # redirect: ok — test\n    return RedirectResponse(url)\n"


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "OPEN_REDIRECT_GATE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, body: str) -> None:
    d = root / "services" / "svc" / "app" / "api"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.py").write_text(body, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _BAD)
        assert _run(root) == 1, "an unguarded non-literal redirect must FAIL"
        _mk(root, _GUARDED)
        assert _run(root) == 0, "an inline host-allowlist guard must PASS"
        _mk(root, _LITERAL)
        assert _run(root) == 0, "a literal redirect target must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# redirect: ok` marker must re-exempt"
    print("selftest_open_redirect: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
