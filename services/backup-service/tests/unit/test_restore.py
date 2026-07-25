"""Restoring a backup — the dangerous action, made deliberate and undoable.

The load-bearing tests are the guarantees that keep a restore from becoming a catastrophe: it
refuses to restore an archive that was never verified, it snapshots the current state before
overwriting (and refuses to overwrite if it cannot), it never aborts the whole restore because one
target failed, and every restore is recorded and can be undone.
"""
from __future__ import annotations

import asyncio
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core import internal_http
from app.core.restore import FAILED, RESTORED, SKIPPED, RestoreOrchestrator
from app.core.restores import RestoreStore
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
    return {"format": "base-backup/v1", "service": service, "taken_at": "2026-07-22T00:00:00Z",
            "tenant": "", "row_counts": {"invoices": 2}, "sealed": "v1:gcm:x", "checksum": "sha256:x"}


def _seed_run(st, objects, run_id="run_1", *, service="billing", verified=True, status="ok"):
    """A recorded backup run with one target, and its archive present in the object store."""
    key = f"{run_id}/{service}.json"
    objects.blobs[key] = json.dumps(_archive(service)).encode("utf-8")
    run(RunStore(st).record({
        "id": run_id, "started_at": "2026-07-22T00:00:00Z", "status": "complete",
        "results": [{"service": service, "status": status, "verified": verified,
                     "archive_key": key, "row_counts": {"invoices": 2}}]}))
    return run_id


async def _token(ref):
    return "tok"


_ISO = ["2026-07-22T01:00:00Z"]


def _iso():
    return _ISO[0]


def _orch(st, objects, responder):
    import app.core.restore as mod

    def fake_post(url, payload, token, *, allowed_hosts, timeout_seconds):
        return responder(url, payload)

    mod.post_json = fake_post
    return RestoreOrchestrator(
        targets=TargetStore(st), object_store=objects, runs=RunStore(st),
        restores=RestoreStore(st), resolve_token=_token, iso=_iso,
        cfg={"egress_allowed_hosts": frozenset(), "target_timeout_seconds": 5})


def _register(st, service="billing"):
    run(TargetStore(st).upsert(Target(service=service, base_url=f"https://{service}.internal",
                                      token_ref="r")))


def _fleet(*, on_import=lambda p: (200, {"inserted": 2, "overwritten": 0, "skipped": 0}),
           on_export=lambda p: (200, _archive())):
    calls = []

    def responder(url, payload):
        calls.append((url, payload))
        if url.endswith("/backup/export"):
            return on_export(payload)
        if url.endswith("/backup/import"):
            return on_import(payload)
        raise AssertionError(f"unexpected call {url}")
    responder.calls = calls
    return responder


# ── plan writes nothing and tells the truth about eligibility ─────────────────
def test_plan_marks_a_verified_target_eligible_and_an_unverified_one_blocked():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _seed_run(st, objects, "run_2", service="wallet", verified=False)
    orch = _orch(st, objects, _fleet())

    ok = run(orch.plan("run_1"))
    assert ok["found"] and ok["targets"][0]["eligible"] is True

    blocked = run(orch.plan("run_2"))
    t = blocked["targets"][0]
    assert t["eligible"] is False and "never verified" in t["blocked_reason"]


def test_plan_of_an_unknown_run_is_found_false():
    st, objects = MemoryDataStore(), _Objects()
    assert run(_orch(st, objects, _fleet()).plan("nope"))["found"] is False


# ── the killer guarantee: never restore an unverified archive ─────────────────
def test_an_unverified_archive_is_not_restored():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=False)
    _register(st, "billing")
    fleet = _fleet()
    result = run(_orch(st, objects, fleet).restore("run_1"))
    assert result["skipped"] == ["billing"] and result["restored"] == []
    # Nothing was imported — an unverified backup must not overwrite live data.
    assert not any(u.endswith("/backup/import") for u, _ in fleet.calls)


def test_forcing_require_verified_false_restores_even_an_unverified_archive():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=False)
    _register(st, "billing")
    result = run(_orch(st, objects, _fleet()).restore("run_1", require_verified=False))
    assert result["restored"] == ["billing"]


# ── snapshot before overwrite, and refuse to overwrite without one ────────────
def test_a_snapshot_is_taken_before_the_import():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")
    fleet = _fleet()
    result = run(_orch(st, objects, fleet).restore("run_1"))
    order = [u.rsplit("/", 1)[-1] for u, _ in fleet.calls]
    assert order == ["export", "import"], "the pre-restore snapshot must precede the import"
    assert result["results"][0]["snapshot_key"], "no undo point was recorded"


def test_a_target_is_not_overwritten_if_its_snapshot_cannot_be_taken():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")

    def export_down(p):
        raise internal_http.TargetUnreachable("cannot snapshot")

    fleet = _fleet(on_export=export_down)
    result = run(_orch(st, objects, fleet).restore("run_1"))
    assert result["failed"] == ["billing"] and result["status"] == "failed"
    assert not any(u.endswith("/backup/import") for u, _ in fleet.calls), "overwrote without an undo point"


def test_snapshot_can_be_waived_explicitly():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")
    fleet = _fleet()
    result = run(_orch(st, objects, fleet).restore("run_1", safety_snapshot=False))
    assert result["restored"] == ["billing"]
    assert [u.rsplit("/", 1)[-1] for u, _ in fleet.calls] == ["import"]


# ── one target failing never aborts the rest ──────────────────────────────────
def test_one_failing_target_does_not_deny_the_others():
    st, objects = MemoryDataStore(), _Objects()
    key_b = "run_x/billing.json"; key_w = "run_x/wallet.json"
    objects.blobs[key_b] = json.dumps(_archive("billing")).encode()
    objects.blobs[key_w] = json.dumps(_archive("wallet")).encode()
    run(RunStore(st).record({
        "id": "run_x", "started_at": "t", "status": "complete", "results": [
            {"service": "billing", "status": "ok", "verified": True, "archive_key": key_b},
            {"service": "wallet", "status": "ok", "verified": True, "archive_key": key_w}]}))
    _register(st, "billing"); _register(st, "wallet")

    def responder(url, payload):
        if url.endswith("/backup/export"):
            return (200, _archive())
        if "wallet" in url and url.endswith("/backup/import"):
            return (500, {"detail": "boom"})
        return (200, {"inserted": 2})

    result = run(_orch(st, objects, responder).restore("run_x"))
    assert set(result["restored"]) == {"billing"} and result["failed"] == ["wallet"]
    assert result["status"] == "partial"


# ── restore-as-clone: the tenant remap is passed through ──────────────────────
def test_into_tenant_is_forwarded_to_the_importer():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")
    fleet = _fleet()
    run(_orch(st, objects, fleet).restore("run_1", into_tenant="beta"))
    imp = [p for u, p in fleet.calls if u.endswith("/backup/import")][0]
    assert imp["into_tenant"] == "beta"


# ── the restore is recorded, and can be undone ────────────────────────────────
def test_the_restore_is_recorded():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")
    result = run(_orch(st, objects, _fleet()).restore("run_1", actor="op1"))
    stored = run(RestoreStore(st).get(result["id"]))
    assert stored is not None and stored["from_run"] == "run_1" and stored["actor"] == "op1"


def test_undo_reapplies_the_pre_restore_snapshot_with_overwrite():
    st, objects = MemoryDataStore(), _Objects()
    _seed_run(st, objects, service="billing", verified=True)
    _register(st, "billing")
    fleet = _fleet()
    restored = run(_orch(st, objects, fleet).restore("run_1"))

    undo = run(_orch(st, objects, fleet).undo(restored["id"]))
    assert undo["restored"] == ["billing"] and undo["undoes"] == restored["id"]
    # The undo import forces overwrite — it is putting back exactly what was there before.
    undo_imports = [p for u, p in fleet.calls if u.endswith("/backup/import")]
    assert undo_imports[-1]["overwrite"] is True


def test_undo_of_an_unknown_restore_raises():
    import pytest
    from app.core.restore import RestoreError
    st, objects = MemoryDataStore(), _Objects()
    with pytest.raises(RestoreError):
        run(_orch(st, objects, _fleet()).undo("nope"))
