"""Retention — prune old backups without ever deleting a service's only working one.

The one test that matters: a target's most recent VERIFIED backup is retained however old, even when
it is far past the retention window, because it is the thing a restore would actually use. The rest
confirm the sweep bounds storage and deletes the archives of the runs it drops.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.retention import RetentionSweeper
from app.core.runs import RunStore


def run(c):
    return asyncio.run(c)


class _Objects:
    def __init__(self):
        self.blobs = {}

    async def put(self, key, data, content_type=None):
        self.blobs[key] = data

    async def delete(self, key):
        return self.blobs.pop(key, None) is not None


_NOW = ["2026-07-25T12:00:00Z"]


def _iso():
    return _NOW[0]


def _seed(st, objects, run_id, started_at, results):
    for r in results:
        if r.get("archive_key"):
            objects.blobs[r["archive_key"]] = b"{}"
    run(RunStore(st).record({"id": run_id, "started_at": started_at, "status": "complete",
                             "results": results}))


def _r(service, key, *, verified=True):
    return {"service": service, "status": "ok", "verified": verified, "archive_key": key}


def _sweeper(st, objects):
    return RetentionSweeper(runs=RunStore(st), object_store=objects, iso=_iso)


def test_runs_beyond_the_keep_count_are_pruned():
    st, objects = MemoryDataStore(), _Objects()
    for i in range(5):
        _seed(st, objects, f"r{i}", f"2026-07-2{i}T02:00:00Z", [_r("billing", f"a{i}.json")])
    result = run(_sweeper(st, objects).prune(keep_runs=3, max_age_seconds=0, scan_limit=100))
    # Newest 3 kept; of the 2 oldest, r0 holds no last-good (r4 does), so r0 is pruned; but the
    # last VERIFIED billing backup is r4 (newest) which is inside keep anyway.
    assert result["pruned_count"] == 2
    assert set(result["pruned"]) == {"r0", "r1"}
    assert "a0.json" not in objects.blobs and "a1.json" not in objects.blobs


def test_a_targets_last_verified_backup_is_never_pruned_even_when_old():
    """The inviolable rule. billing's ONLY verified backup is the oldest run; retention must keep it
    however far past the window it falls."""
    st, objects = MemoryDataStore(), _Objects()
    # r0 (oldest) is billing's only verified backup. r1..r4 are wallet's, billing missed in them.
    _seed(st, objects, "r0", "2026-07-20T02:00:00Z", [_r("billing", "b.json")])
    for i in range(1, 5):
        _seed(st, objects, f"r{i}", f"2026-07-2{i}T02:00:00Z", [_r("wallet", f"w{i}.json")])
    result = run(_sweeper(st, objects).prune(keep_runs=2, max_age_seconds=0, scan_limit=100))
    assert "r0" in result["kept_protected"], "deleted a target's only working backup"
    assert "b.json" in objects.blobs
    assert run(RunStore(st).get("r0")) is not None


def test_max_age_prunes_old_runs_but_still_spares_the_last_good():
    st, objects = MemoryDataStore(), _Objects()
    _seed(st, objects, "old_good", "2026-07-10T02:00:00Z", [_r("billing", "g.json")])
    _seed(st, objects, "old_dup", "2026-07-11T02:00:00Z", [_r("billing", "d.json")])
    _seed(st, objects, "fresh", "2026-07-25T02:00:00Z", [_r("wallet", "f.json")])
    # 3 days = 259200s. old_* are ~14 days old; fresh is 10h old.
    result = run(_sweeper(st, objects).prune(keep_runs=1, max_age_seconds=259_200, scan_limit=100))
    # 'fresh' is billing? no — billing's last-good is old_dup (newer than old_good). old_dup spared;
    # old_good pruned (billing already protected by old_dup); fresh kept (inside keep_runs).
    assert "old_dup" in result["kept_protected"]
    assert "old_good" in result["pruned"]
    assert "g.json" not in objects.blobs and "d.json" in objects.blobs


def test_nothing_to_prune_is_a_clean_noop():
    st, objects = MemoryDataStore(), _Objects()
    _seed(st, objects, "r0", "2026-07-25T02:00:00Z", [_r("billing", "a.json")])
    result = run(_sweeper(st, objects).prune(keep_runs=30, max_age_seconds=0, scan_limit=100))
    assert result["pruned_count"] == 0 and objects.blobs
