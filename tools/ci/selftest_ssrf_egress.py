#!/usr/bin/env python3
"""Mutation self-test for safety:ssrf-egress — a gate that cannot fail is decoration.

Confirms: a payment_http transport doing a raw urlopen with NO egress guard FAILS; adding a
guard_fetch_url call PASSES; and a `# ssrf: ok` marker re-exempts.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_ssrf_egress.py"

_BAD = ("import urllib.request\n"
        "def _send(url):\n"
        "    return urllib.request.urlopen(url)\n")
_GOOD = ("import urllib.request\n"
         "def guard_fetch_url(url):\n    return None\n"
         "def _send(url):\n"
         "    if guard_fetch_url(url):\n        return 0, {}\n"
         "    return urllib.request.urlopen(url)\n")
_MARKED = ("import urllib.request\n"
           "def _send(url):\n"
           "    # ssrf: ok — test\n"
           "    return urllib.request.urlopen(url)\n")


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "SSRF_GATE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, body: str) -> None:
    d = root / "services" / "svc" / "app" / "adapters"
    d.mkdir(parents=True, exist_ok=True)
    (d / "payment_http.py").write_text(body, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _BAD)
        assert _run(root) == 1, "a payment transport with a raw fetch and no guard must FAIL"
        _mk(root, _GOOD)
        assert _run(root) == 0, "an egress-guarded transport must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# ssrf: ok` marker must re-exempt"
    print("selftest_ssrf_egress: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
