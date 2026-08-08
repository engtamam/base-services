#!/usr/bin/env python
"""Prove safety:pool-config FAILS on the eager default and on a too-large floor, PASSES when sized."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_pool_config.py"

SIZED_PG = 'x = create_pool(\n    self._dsn, min_size=self._pool_min, max_size=self._pool_max)\n'
BARE_PG = 'x = create_pool(self._dsn)\n'
GOOD_CFG = "    DATASTORE_POOL_MIN: int = 1\n"
BIG_CFG = "    DATASTORE_POOL_MIN: int = 10\n"


def _repo(tmp: pathlib.Path, pg: str, cfg: str) -> None:
    d = tmp / "services" / "demo-service" / "app"
    (d / "adapters" / "datastore").mkdir(parents=True)
    (d / "adapters" / "datastore" / "postgres.py").write_text(pg, encoding="utf-8")
    (d / "config.py").write_text(cfg, encoding="utf-8")


def _run(tmp: pathlib.Path) -> int:
    env = dict(os.environ, POOL_CONFIG_GATE_ROOT=str(tmp))
    return subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, env=env).returncode


def main() -> int:
    cases = [
        ("sized + small default -> pass", SIZED_PG, GOOD_CFG, 0),
        ("bare create_pool -> fail", BARE_PG, GOOD_CFG, 1),
        ("sized but eager floor default -> fail", SIZED_PG, BIG_CFG, 1),
    ]
    fails = []
    for name, pg, cfg, want in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            _repo(tmp, pg, cfg)
            rc = _run(tmp)
        if rc != want:
            fails.append(f"  {name}: expected {want}, got {rc}")
    if fails:
        print("pool-config selftest FAILED:\n" + "\n".join(fails))
        return 1
    print(f"      pool-config selftest OK — {len(cases)} cases as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
