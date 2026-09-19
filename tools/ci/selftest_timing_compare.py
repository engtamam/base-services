#!/usr/bin/env python3
"""Mutation self-test for safety:timing-compare — a gate that cannot fail is decoration.

Confirms: a var-to-var `supplied == secret` FAILS; hmac.compare_digest PASSES; a compare to an
ALL-CAPS sentinel constant (mode == PASSWORD) PASSES; and a `# timing: ok` marker re-exempts.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_timing_compare.py"

_BAD = "def f(supplied, secret):\n    return supplied == secret\n"
_GOOD = "import hmac\ndef f(supplied, secret):\n    return hmac.compare_digest(supplied, secret)\n"
_SENTINEL = "PASSWORD = 'p'\ndef f(mode):\n    return mode == PASSWORD\n"
_MARKED = "def f(supplied, secret):\n    return supplied == secret  # timing: ok — test\n"


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "TIMING_COMPARE_GATE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, body: str) -> None:
    core = root / "services" / "svc" / "app" / "core"
    core.mkdir(parents=True, exist_ok=True)
    (core / "x.py").write_text(body, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _BAD)
        assert _run(root) == 1, "a var-to-var secret == compare must FAIL"
        _mk(root, _GOOD)
        assert _run(root) == 0, "hmac.compare_digest must PASS"
        _mk(root, _SENTINEL)
        assert _run(root) == 0, "a compare to an ALL-CAPS sentinel constant must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# timing: ok` marker must re-exempt"
    print("selftest_timing_compare: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
