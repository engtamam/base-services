#!/usr/bin/env python3
"""Mutation self-test for safety:aesgcm-aad — a gate that cannot fail is decoration.

Confirms: an AEAD encrypt/decrypt with associated_data=None FAILS; passing an AAD PASSES; and a
`# aad: ok` marker re-exempts.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_aesgcm_aad.py"

_BAD = ("def f(key, iv, data):\n"
        "    from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
        "    return AESGCM(key).encrypt(iv, data, None)\n")
_GOOD = ("def f(key, iv, data, aad):\n"
         "    from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
         "    return AESGCM(key).encrypt(iv, data, aad)\n")
_MARKED = ("def f(key, iv, data):\n"
           "    from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
           "    # aad: ok — test\n"
           "    return AESGCM(key).encrypt(iv, data, None)\n")


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "AESGCM_AAD_GATE_ROOT": str(root)}
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
        assert _run(root) == 1, "an AEAD seal with associated_data=None must FAIL"
        _mk(root, _GOOD)
        assert _run(root) == 0, "passing an AAD must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# aad: ok` marker must re-exempt"
    print("selftest_aesgcm_aad: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
