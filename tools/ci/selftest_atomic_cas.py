#!/usr/bin/env python3
"""Mutation self-test for safety:atomic-cas — a gate that cannot fail is decoration.

Confirms: a same-key get→put with no atomic write FAILS; routing the write through cas() PASSES; and
an `# atomic: ok` marker (on the put, or a comment directly above it) re-exempts it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_atomic_cas.py"

_BAD = '''
class S:
    async def f(self, key):
        row = await self._store.get("c", key)
        row["n"] = 1
        await self._store.put("c", key, row)
'''
_CAS = '''
class S:
    async def f(self, key):
        row = await self._store.get("c", key)
        row["n"] = 1
        await self._store.cas("c", key, 0, row)
'''
_MARKED = '''
class S:
    async def f(self, key):
        row = await self._store.get("c", key)
        row["n"] = 1
        # atomic: ok — test
        await self._store.put("c", key, row)
'''


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "ATOMIC_CAS_GATE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, body: str) -> None:
    core = root / "services" / "svc" / "app" / "core"
    core.mkdir(parents=True, exist_ok=True)
    (core / "stores.py").write_text(body, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _BAD)
        assert _run(root) == 1, "an unguarded same-key read-modify-write must FAIL"
        _mk(root, _CAS)
        assert _run(root) == 0, "routing through cas() must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "an `# atomic: ok` marker must re-exempt"
    print("selftest_atomic_cas: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
