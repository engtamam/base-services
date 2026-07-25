"""The restore drill — proving, on a schedule, that every service still restores from its best backup.

The load-bearing tests: a drill re-verifies the AT-REST stored bytes (so a backup that verified at
capture but rotted since is caught), a target with no verified backup is a drill FAILURE (not a
silent pass), and the drill never touches live data — it only reads the archive back and asks the
target to test-restore it in its own throwaway namespace.
"""
from __future__ import annotations

import asyncio
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core import internal_http
from app.core.drill import FAILED, PASSED, RestoreDrill
from app.core.drills import DrillStore
from app.core.runs import RunStore
from app.core.targets import Target, TargetStore


def run(c):
    return asyncio.run(c)


class _Objects:
    def __init__(self):
        self.blobs = {}

    async def put(self, key, data, content_type=None):
        self.blobs[key] = data

    async def get(self, key):
        return self.blobs[key]


def _archive(service="billing"):
    return {"format": "base-backup/v1", "service": service, "sealed": "v1:gcm:x", "checksum": "x"}


_ISO = ["2026-07-25T04:00:00Z"]


def _iso():
    return _ISO[0]


async def _token(ref):
    return "tok"


def _seed_run(st, objects, run_id, started_at, service, *, verified=True):
    key = f"{run_id}/{service}.json"
    objects.blobs[key] = json.dumps(_archive(service)).encode()
    run(RunStore(st).record({"id": run_id, "started_at": started_at, "status": "complete",
                             "results": [{"service": service, "status": "ok", "verified": verified,
                                          "archive_key": key}]}))


def _register(st, *svcs):
    for s in svcs:
        run(TargetStore(st).upsert(Target(service=s, base_url=f"https://{s}.internal", token_ref="r")))


def _drill(st, objects, responder):
    import app.core.drill as mod
    mod.post_json = lambda url, payload, token, **kw: responder(url, payload)
    return RestoreDrill(targets=TargetStore(st), object_store=objects, runs=RunStore(st),
                        drills=DrillStore(st), resolve_token=_token, iso=_iso,
                        cfg={"egress_allowed_hosts": frozenset(), "target_timeout_seconds": 5,
                             "target_limit": 500, "run_history": 200})


# ── a healthy drill: the best backup still restores ───────────────────────────
def test_a_drill_passes_when_every_best_backup_still_restores():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "r1", "2026-07-25T02:00:00Z", "billing")
    _register(st, "billing")
    calls = []

    def responder(url, payload):
        calls.append(url)
        return (200, {"verified": True, "checked": 5})

    report = run(_drill(st, objects, responder).run())
    assert report["status"] == "passed" and report["passed"] == ["billing"]
    # It re-verified against the target — the drill is an ACTIVE re-check, not a record read.
    assert calls and calls[0].endswith("/internal/backup/verify")


# ── the drill's whole reason: at-rest rot is caught ───────────────────────────
def test_a_backup_that_no_longer_restores_is_a_drill_failure():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "r1", "2026-07-25T02:00:00Z", "billing")
    _register(st, "billing")

    def rotted(url, payload):
        return (200, {"verified": False, "mismatches": ["widgets/w1: differs at rest"]})

    report = run(_drill(st, objects, rotted).run())
    assert report["failed"] == ["billing"] and report["status"] == "failed"
    assert "no longer restores" in report["results"][0]["error"].lower()


def test_a_target_with_no_verified_backup_is_a_drill_failure_not_a_pass():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "r1", "2026-07-25T02:00:00Z", "billing", verified=False)  # never verified
    _register(st, "billing")
    report = run(_drill(st, objects, lambda u, p: (200, {"verified": True})).run())
    assert report["failed"] == ["billing"]
    assert "no verified backup" in report["results"][0]["error"]


def test_a_target_unreachable_during_the_drill_fails_that_target():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "r1", "2026-07-25T02:00:00Z", "billing")
    _register(st, "billing")

    def down(url, payload):
        raise internal_http.TargetUnreachable("connection refused")

    report = run(_drill(st, objects, down).run())
    assert report["failed"] == ["billing"] and "unreachable" in report["results"][0]["error"]


def test_the_drill_uses_the_most_recent_verified_backup():
    """Two runs; the newer one missed billing. The drill must use the older VERIFIED archive."""
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "old", "2026-07-24T02:00:00Z", "billing")            # verified
    # newer run where billing was missed (no verified archive)
    run(RunStore(st).record({"id": "new", "started_at": "2026-07-25T02:00:00Z", "status": "incomplete",
                             "results": [{"service": "billing", "status": "missed", "verified": False}]}))
    _register(st, "billing")
    report = run(_drill(st, objects, lambda u, p: (200, {"verified": True, "checked": 3})).run())
    assert report["passed"] == ["billing"]
    assert report["results"][0]["from_run"] == "old"


def test_the_drill_is_recorded():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, "r1", "2026-07-25T02:00:00Z", "billing")
    _register(st, "billing")
    report = run(_drill(st, objects, lambda u, p: (200, {"verified": True})).run())
    assert run(DrillStore(st).get(report["id"])) is not None
