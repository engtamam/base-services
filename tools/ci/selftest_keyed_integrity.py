#!/usr/bin/env python3
"""Mutation self-test for safety:keyed-integrity — a gate that cannot fail is decoration.

Confirms: the real fleet passes; an unkeyed default (hash-chain / none / "") fails; and an
`# keyed-integrity: ok` marker re-exempts it.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_keyed_integrity.py"

_GOOD = 'INTEGRITY_DRIVER: str = "hmac-chain"\n'
_BAD = 'INTEGRITY_DRIVER: str = "hash-chain"\n'
_BAD_EXEMPT = 'INTEGRITY_DRIVER: str = "hash-chain"  # keyed-integrity: ok INTEGRITY_DRIVER — test\n'


def _run(root: pathlib.Path) -> int:
    env = {"KEYED_INTEGRITY_GATE_ROOT": str(root)}
    import os
    env = {**os.environ, **env}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, body: str) -> None:
    cfg = root / "services" / "svc" / "app"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.py").write_text("class Settings:\n    " + body, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _GOOD)
        assert _run(root) == 0, "keyed default must PASS"
        _mk(root, _BAD)
        assert _run(root) == 1, "unkeyed default must FAIL"
        _mk(root, _BAD_EXEMPT)
        assert _run(root) == 0, "an explicit marker must re-exempt"
    print("selftest_keyed_integrity: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
