#!/usr/bin/env python
"""Gate: the postgres pool is sized from config, never left at asyncpg's eager default.

The failure this locks out is subtle and was catastrophic. `asyncpg.create_pool(dsn)` with no
arguments takes min_size=10 — an EAGER-but-lazy floor: the pool fills to ten on a service's first
query and HOLDS there for the process lifetime. Twenty-four services then pin 240 backend connections
against a single postgres whose stock ceiling is 100, and the fleet deadlocks on an ordinary request
once enough services have been touched — and, with maxUnavailable:0, a rollout deadlocks outright. It
is a default nobody chose, invisible until production, and correlated with success (the more of the
platform a customer uses, the closer they are to it).

So every postgres adapter MUST size its pool from settings (min defaulting to 1, lazy and small), and
every config MUST declare the knobs with a small default. This gate keeps both true, so the eager
default cannot creep back in the next copy-paste of the adapter.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("POOL_CONFIG_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# The bare, argument-less pool creation — the eager default. Its presence is the bug.
_BARE = re.compile(r"create_pool\(\s*self\._dsn\s*\)")
# The sized form must read the min from an instance field fed by settings.
_SIZED = "min_size=self._pool_min"
# The config knob and a sane (small, non-eager) default.
_KNOB = re.compile(r"DATASTORE_POOL_MIN\s*:\s*int\s*=\s*(\d+)")
_MAX_SANE_MIN_DEFAULT = 2   # a floor above this per service is the eager-default problem again


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    checked = 0
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        pg = svc / "app" / "adapters" / "datastore" / "postgres.py"
        if not pg.is_file():
            continue
        checked += 1
        text = pg.read_text(encoding="utf-8")
        if _BARE.search(text):
            problems.append(f"{svc.name}: postgres.py calls create_pool(self._dsn) with no sizing — "
                            f"that is asyncpg's eager min_size=10 default. Size it from settings.")
        if _SIZED not in text:
            problems.append(f"{svc.name}: postgres.py does not size the pool from settings "
                            f"(expected min_size=self._pool_min).")
        cfg = svc / "app" / "config.py"
        if cfg.is_file():
            m = _KNOB.search(cfg.read_text(encoding="utf-8"))
            if not m:
                problems.append(f"{svc.name}: config.py declares no DATASTORE_POOL_MIN — the pool "
                                f"size is not configurable.")
            elif int(m.group(1)) > _MAX_SANE_MIN_DEFAULT:
                problems.append(f"{svc.name}: DATASTORE_POOL_MIN defaults to {m.group(1)} — a floor "
                                f"that large per service is the eager-default problem again. Keep it "
                                f"lazy (<= {_MAX_SANE_MIN_DEFAULT}); grow max, not the sticky floor.")
    if problems:
        print(f"safety:pool-config — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:pool-config OK — {checked} postgres adapter(s) size the pool from settings; "
          f"no eager default")
    return 0


if __name__ == "__main__":
    sys.exit(main())
