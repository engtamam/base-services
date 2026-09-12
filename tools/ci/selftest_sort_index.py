#!/usr/bin/env python3
"""Mutation self-test for safety:sort-index — a gate that cannot fail is decoration.

Confirms: a sort field with a matching index PASSES; the same sort with the index removed FAILS; and
a `# sort-index: ok <field>` marker re-exempts it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

CHECK = pathlib.Path(__file__).resolve().parent / "check_sort_index.py"

_PG_INDEXED = '''_TABLE = "kv_store"
_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS kv_paid_at_desc ON {_TABLE} "
    f"(collection, COALESCE(value->'paid_at', 'null'::jsonb) DESC, key DESC)",
)
'''
_PG_BARE = '''_TABLE = "kv_store"
_INDEXES = ()
'''
_PG_MARKED = '''_TABLE = "kv_store"
# sort-index: ok paid_at — bounded
_INDEXES = ()
'''
_STORE = 'async def f(self):\n    return await self._s.find("c", {"t": 1}, sort=("paid_at", "desc"))\n'


def _run(root: pathlib.Path) -> int:
    env = {**os.environ, "SORT_INDEX_GATE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CHECK)], env=env,
                          capture_output=True, text=True).returncode


def _mk(root: pathlib.Path, pg: str) -> None:
    svc = root / "services" / "svc" / "app"
    (svc / "adapters" / "datastore").mkdir(parents=True, exist_ok=True)
    (svc / "core").mkdir(parents=True, exist_ok=True)
    (svc / "adapters" / "datastore" / "postgres.py").write_text(pg, encoding="utf-8")
    (svc / "core" / "stores.py").write_text(_STORE, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _mk(root, _PG_INDEXED)
        assert _run(root) == 0, "an indexed sort field must PASS"
        _mk(root, _PG_BARE)
        assert _run(root) == 1, "an unindexed sort field must FAIL"
        _mk(root, _PG_MARKED)
        assert _run(root) == 0, "a marker must re-exempt"
    print("selftest_sort_index: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
