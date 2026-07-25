"""The edge cases a backup system must survive — one test per row of the phase's edge-case table.

These are the failures that turn "we have backups" into a false statement at the worst moment: a
half-written archive, a service down during a run, clock skew, a partial restore re-run, storage
full mid-run, a rotated KEK, a huge collection, corruption at rest. Each is proven here, and each of
the four the phase names is mutation-tested (see tests/MUTATION-46.7.md) to prove the guard has teeth.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B
from app.core import internal_http
from app.core.orchestrator import FAILED, MISSED, OK, Orchestrator
from app.core.runs import RunStore
from app.core.targets import Target, TargetStore


def run(c):
    return asyncio.run(c)


# ── shared fakes ──────────────────────────────────────────────────────────────
class _Objects:
    def __init__(self, put_fails_for=()):
        self.blobs = {}
        self._put_fails_for = set(put_fails_for)

    async def put(self, key, data, content_type=None):
        if any(s in key for s in self._put_fails_for):
            raise OSError("No space left on device")
        self.blobs[key] = data

    async def get(self, key):
        return self.blobs[key]


def _sealed(service="billing", kek="backup-key"):
    seal, _ = B.make_sealer(kek)
    body = seal(json.dumps({"service": service, "version": "1", "taken_at": "t", "tenant": "",
                            "collections": {"widgets": [{"id": "w1", "tenant": "acme", "n": 1}]}},
                           separators=(",", ":"), sort_keys=True))
    return {"format": "base-backup/v1", "service": service, "taken_at": "2099-01-01T00:00:00Z",
            "tenant": "", "row_counts": {"widgets": 1}, "sealed": body,
            "checksum": "sha256:" + __import__("hashlib").sha256(body.encode()).hexdigest()}


_ISO = ["2026-07-25T09:00:00Z"]


def _iso():
    return _ISO[0]


async def _token(ref):
    return "tok"


def _orch(st, objects, responder):
    import app.core.orchestrator as mod
    mod.post_json = lambda url, payload, token, **kw: responder(url, payload)
    return Orchestrator(targets=TargetStore(st), object_store=objects, runs=RunStore(st),
                        resolve_token=_token, clock=_iso, iso=_iso,
                        cfg={"egress_allowed_hosts": frozenset(), "target_timeout_seconds": 5,
                             "target_limit": 500})


def _register(st, *svcs):
    for s in svcs:
        run(TargetStore(st).upsert(Target(service=s, base_url=f"https://{s}.internal", token_ref="r")))


def _fleet_ok(export):
    def responder(url, payload):
        if url.endswith("/backup/verify"):
            return (200, {"verified": True, "checked": 1})
        return export(url, payload)
    return responder


# ── 1. process dies mid-write: the checksum is written last ───────────────────
def test_a_truncated_archive_missing_its_checksum_is_refused():
    """A write that died loses the trailing checksum; verify() must refuse it, never half-restore."""
    archive = _sealed()
    del archive["checksum"]
    with pytest.raises(B.BackupError):
        B.verify(archive)


def test_an_archive_missing_its_sealed_body_is_a_clean_refusal_not_a_crash():
    """A truncation that took the body too must yield a clear BackupError, not an AttributeError from
    checksumming None — the guard exists so the failure is legible, not a 500 with a stack trace."""
    archive = _sealed()
    archive["sealed"] = None                 # JSON null — a truncation that took the body
    with pytest.raises(B.BackupError):
        B.verify(archive)


def test_the_orchestrator_does_not_store_a_checksum_failed_archive():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    bad = _sealed(); bad["checksum"] = "sha256:00"
    result = run(_orch(st, objects, _fleet_ok(lambda u, p: (200, bad))).run())
    assert result["failed"] == ["billing"] and objects.blobs == {}


# ── 2. a service down during a run: MISSED, never skipped ─────────────────────
def test_a_service_down_during_a_run_is_missed_and_makes_the_run_incomplete():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "up", "down")

    def export(url, payload):
        if "down" in url:
            raise internal_http.TargetUnreachable("connection refused")
        return (200, _sealed("up"))

    result = run(_orch(st, objects, _fleet_ok(export)).run())
    assert result["missed"] == ["down"] and result["status"] == "incomplete"
    assert result["ok"] == 1               # the reachable one still captured


# ── 3. clock skew: the run's point-in-time is the orchestrator's, stamped once ─
def test_the_run_timestamp_is_the_orchestrators_clock_not_the_targets():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "a", "b")
    # Both targets report a wildly wrong taken_at (2099); the run must ignore it.
    result = run(_orch(st, objects, _fleet_ok(lambda u, p: (200, _sealed()))).run())
    assert result["started_at"] == _iso()
    assert result["id"].startswith("run_") and "2099" not in result["id"]


# ── 4. restore into a partly-existing tenant / re-run: idempotent, no clobber ──
def test_a_restore_re_run_changes_nothing_and_does_not_clobber_newer_rows():
    seal, unseal = B.make_sealer("k")
    archive = run(B.export(_seed_widgets(MemoryDataStore()), _DECL, service="s", version="1",
                           taken_at="t", seal=seal, tenant="", scan_cap=100))
    dst = MemoryDataStore()
    first = run(B.import_archive(dst, archive, unseal=unseal))
    assert first["inserted"] == 2

    # A row written AFTER the backup must survive a restore (no clobber without overwrite).
    run(dst.put("widgets", "w0", {"id": "w0", "tenant": "acme", "n": 999}))
    second = run(B.import_archive(dst, archive, unseal=unseal))
    assert second["inserted"] == 0 and second["skipped"] == 2      # re-run is a no-op
    assert run(dst.get("widgets", "w0"))["n"] == 999               # newer row untouched


# ── 5. storage full mid-run: that target fails, the run does NOT abort ─────────
def test_storage_full_fails_one_target_but_not_the_whole_run():
    st = MemoryDataStore()
    objects = _Objects(put_fails_for=("wallet",))     # wallet's archive cannot be stored
    _register(st, "billing", "wallet")
    result = run(_orch(st, objects, _fleet_ok(lambda u, p: (200, _sealed()))).run())
    assert "wallet" in result["failed"]
    assert "billing" in result["ok"] if isinstance(result["ok"], list) else result["ok"] == 1
    # billing's archive was still stored — one target's storage failure did not deny the other.
    assert any("billing" in k for k in objects.blobs)
    assert not any("wallet" in k for k in objects.blobs)


# ── 6. KEK rotated since the backup: restore refuses rather than garbage ───────
def test_a_restore_with_the_wrong_backup_key_is_refused():
    seal, _ = B.make_sealer("the-key-it-was-sealed-with")
    archive = run(B.export(_seed_widgets(MemoryDataStore()), _DECL, service="s", version="1",
                           taken_at="t", seal=seal, tenant="", scan_cap=100))
    _, wrong_unseal = B.make_sealer("a-different-key-after-rotation")
    with pytest.raises(B.BackupError):
        run(B.import_archive(MemoryDataStore(), archive, unseal=wrong_unseal))


# ── 7. a huge collection: per-tenant export PAGES via find, never scan_all ─────
def test_a_per_tenant_export_pages_via_find_and_never_scans(monkeypatch):
    st = MemoryDataStore()
    for i in range(1200):                              # > _PAGE (500): forces multiple find pages
        run(st.put("widgets", f"w{i}", {"id": f"w{i}", "tenant": "big", "n": i}))

    async def _no_scan(*a, **k):
        raise AssertionError("scan_all was used on a per-tenant export — it must page via find")

    monkeypatch.setattr(st, "scan_all", _no_scan)
    seal, _ = B.make_sealer("k")
    archive = run(B.export(st, _DECL, service="s", version="1", taken_at="t", seal=seal,
                           tenant="big", scan_cap=100))
    assert archive["row_counts"]["widgets"] == 1200    # every row, paged, without a scan


# ── 8. backup corrupted at rest: caught by the checksum, not the customer ─────
def test_corruption_at_rest_is_caught_by_the_checksum():
    archive = _sealed()
    # Flip the stored ciphertext after the checksum was computed — corruption at rest.
    archive["sealed"] = archive["sealed"][:-4] + "AAAA"
    with pytest.raises(B.BackupError):
        B.verify(archive)


# ── fixtures for the core-backup round-trip cases ─────────────────────────────
_DECL = {"object_prefix": "", "collections": [{"name": "widgets", "tenant_scoped": True}],
         "excluded": []}


def _seed_widgets(st):
    for i in range(2):
        run(st.put("widgets", f"w{i}", {"id": f"w{i}", "tenant": "acme", "n": i}))
    return st
