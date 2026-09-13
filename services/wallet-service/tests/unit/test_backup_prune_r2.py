"""Round-2 backup propagation: import_archive prune makes a restore authoritative (backup-r2-01),
verified on wallet's copy (the composite-key-capable variant)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B


def run(c):
    return asyncio.run(c)


_DECL = {"object_prefix": "", "collections": [{"name": "widgets", "tenant_scoped": False}],
         "excluded": []}


def test_prune_deletes_rows_absent_from_the_archive():
    seal, unseal = B.make_sealer("k")
    src = MemoryDataStore()
    run(src.put("widgets", "w1", {"id": "w1", "n": 1}))
    snapshot = run(B.export(src, _DECL, service="wallet", version="1", taken_at="t",
                            seal=seal, scan_cap=1000))          # snapshot = {w1}

    target = MemoryDataStore()
    run(target.put("widgets", "w1", {"id": "w1", "n": 1}))
    run(target.put("widgets", "w2", {"id": "w2", "n": 2}))      # inserted by a wrong restore

    out = run(B.import_archive(target, snapshot, unseal=unseal, overwrite=True, prune=True,
                              declaration=_DECL, scan_cap=1000))
    assert out["pruned"] == 1
    assert run(target.get("widgets", "w2")) is None             # authoritative rollback
    assert run(target.get("widgets", "w1")) is not None


def test_prune_refuses_a_truncated_current_view():
    seal, unseal = B.make_sealer("k")
    src = MemoryDataStore()
    snapshot = run(B.export(src, _DECL, service="wallet", version="1", taken_at="t",
                            seal=seal, scan_cap=1000))          # empty snapshot
    target = MemoryDataStore()
    for i in range(150):
        run(target.put("widgets", f"w{i}", {"id": f"w{i}"}))
    import pytest
    with pytest.raises(B.BackupError):
        run(B.import_archive(target, snapshot, unseal=unseal, overwrite=True, prune=True,
                            declaration=_DECL, scan_cap=100))
