#!/usr/bin/env python3
"""Mutation self-test for safety:money-float — a gate that cannot fail is decoration.

Confirms: a bare `amount_minor / 100` (a hardcoded-exponent conversion) FAILS; a percent product
`Decimal(subtotal_minor) * Decimal(pct) / 100` and a currency-helper `float(major(amount_minor, cur))`
both PASS (not exponent bugs); and a `# money: ok` marker re-exempts.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_money_float.py"

_BAD = "def f(amount_minor):\n    return amount_minor / 100\n"
_PERCENT = ("from decimal import Decimal\n"
            "def f(subtotal_minor, pct):\n"
            "    return int(Decimal(subtotal_minor) * Decimal(pct) / 100)\n")
_HELPER = "def f(amount_minor, cur):\n    return float(major(amount_minor, cur))\n"
_MARKED = "def f(amount_minor):\n    return amount_minor / 100  # money: ok — test\n"


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "MONEY_FLOAT_GATE_ROOT": str(root)}
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
        assert _run(root) == 1, "a bare amount_minor / 100 exponent conversion must FAIL"
        _mk(root, _PERCENT)
        assert _run(root) == 0, "a Decimal percent product (/100) must PASS"
        _mk(root, _HELPER)
        assert _run(root) == 0, "float(major(...)) currency-helper serialization must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# money: ok` marker must re-exempt"
    print("selftest_money_float: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
