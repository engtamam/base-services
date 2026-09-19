#!/usr/bin/env python3
"""Mutation self-test for safety:cas-create — a gate that cannot fail is decoration.

Confirms: a collection CREATED via put() but version-pinned cas()-updated FAILS; creating it via a
cas insert-if-absent (expected_version=0) PASSES; and a `# cas-create: ok` marker (on the put, or a
comment directly above it) re-exempts it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_cas_create.py"

_BAD = '''
class S:
    async def create(self, k):
        await self._store.put("c", k, {})
    async def bump(self, k, ver):
        await self._store.cas("c", k, ver, {})
'''
_GOOD = '''
class S:
    async def create(self, k):
        await self._store.cas("c", k, 0, {})
    async def bump(self, k, ver):
        await self._store.cas("c", k, ver, {})
'''
_MARKED = '''
class S:
    async def create(self, k):
        # cas-create: ok — test
        await self._store.put("c", k, {})
    async def bump(self, k, ver):
        await self._store.cas("c", k, ver, {})
'''


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "CAS_CREATE_GATE_ROOT": str(root)}
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
        assert _run(root) == 1, "a put-created, version-pinned-cas-updated collection must FAIL"
        _mk(root, _GOOD)
        assert _run(root) == 0, "creating via cas insert-if-absent must PASS"
        _mk(root, _MARKED)
        assert _run(root) == 0, "a `# cas-create: ok` marker must re-exempt"
    print("selftest_cas_create: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
