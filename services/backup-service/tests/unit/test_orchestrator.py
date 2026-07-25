"""Capturing a backup run across registered targets.

The load-bearing test is that a target which could not be reached is recorded as MISSED and makes
the run INCOMPLETE — never silently skipped. "We backed up" while one service was absent is the
exact lie the phase exists to kill. The rest guard the store: never keep an archive a restore would
reject, and never abort the run because one target failed.

AND (Phase 46.4) that a stored archive is not called a backup until it has been PROVEN to restore.
After storing, the orchestrator reads the archive back from the store and asks the target to test-
restore it; a target that cannot prove the restore is UNVERIFIED, and one that restores WRONG is
FAILED. An unverified backup counts as none.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core import internal_http
from app.core.orchestrator import FAILED, MISSED, OK, UNVERIFIED, Orchestrator
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


def _sealed_archive(service="svc", counts=None):
    sealed = "v1:gcm:" + service
    return {"format": "base-backup/v1", "service": service, "taken_at": "2026-07-22T00:00:00Z",
            "tenant": "", "row_counts": counts or {"widgets": 3}, "sealed": sealed,
            "checksum": "sha256:" + hashlib.sha256(sealed.encode()).hexdigest()}


def _fleet(export, *, verify=None):
    """A responder that answers both calls: `export(url,payload)` for /export, and the verify verdict
    for /verify (default: verified, so a test that only cares about export gets a clean happy path)."""
    def responder(url, payload):
        if url.endswith("/backup/verify"):
            return (verify or (lambda u, p: (200, {"verified": True, "checked": 3})))(url, payload)
        return export(url, payload)
    return responder


def _orch(st, objects, responder, cfg=None):
    """responder(url, payload) -> (status, body) — the fake target fleet."""
    import app.core.orchestrator as orch_mod

    def fake_post(url, payload, token, *, allowed_hosts, timeout_seconds):
        return responder(url, payload)

    orch_mod.post_json = fake_post   # patch the module the orchestrator imported
    return Orchestrator(
        targets=TargetStore(st), object_store=objects, runs=RunStore(st),
        resolve_token=_token, clock=_iso, iso=_iso,
        cfg={"egress_allowed_hosts": frozenset(), "target_timeout_seconds": 5,
             "target_limit": 500, **(cfg or {})})


async def _token(ref):
    return "tok"


_ISO = ["2026-07-22T00:00:00Z"]


def _iso():
    return _ISO[0]


def _register(st, *services):
    ts = TargetStore(st)
    for s in services:
        run(ts.upsert(Target(service=s, base_url=f"https://{s}.internal", token_ref="r")))


# ── the run captures what it can, and proves it restores ──────────────────────
def test_a_run_captures_every_target_and_stores_its_archive():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing", "wallet")
    orch = _orch(st, objects, _fleet(lambda url, p: (200, _sealed_archive("billing"))))
    result = run(orch.run())
    assert result["status"] == "complete"
    assert result["ok"] == 2
    assert result["verified"] == 2          # not just stored — proven to restore
    assert len(objects.blobs) == 2


# ── the rule that makes a run honest ──────────────────────────────────────────
def test_an_unreachable_target_is_MISSED_not_skipped():
    """The whole point. A run must not report complete while a service was silently absent."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing", "ghost")

    def export(url, payload):
        if "ghost" in url:
            raise internal_http.TargetUnreachable("connection refused")
        return (200, _sealed_archive("billing"))

    result = run(_orch(st, objects, _fleet(export)).run())
    assert result["status"] == "incomplete", "a missed target did not make the run incomplete"
    assert result["missed"] == ["ghost"]
    assert result["ok"] == 1
    # The reachable one was still captured — a miss does not abort the run.
    assert any("billing" in k for k in objects.blobs)


def test_a_target_with_no_backup_key_is_missed_not_failed():
    """A 503 means the target refused to export in the clear — correct on its side, a miss on ours."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "nokey")
    result = run(_orch(st, objects, _fleet(lambda url, p: (503, {}))).run())
    assert result["missed"] == ["nokey"] and result["status"] == "incomplete"


# ── never store an archive a restore would reject ─────────────────────────────
def test_a_corrupt_archive_is_FAILED_and_not_stored():
    """The store must never hold something a restore would reject — that is how 'we have backups'
    becomes false at the worst moment."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    bad = _sealed_archive("billing")
    bad["checksum"] = "sha256:deadbeef"
    result = run(_orch(st, objects, _fleet(lambda url, p: (200, bad))).run())
    assert result["failed"] == ["billing"] and result["status"] == "failed"
    assert objects.blobs == {}, "a corrupt archive was stored anyway"


def test_a_foreign_format_is_failed():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    result = run(_orch(st, objects, _fleet(lambda url, p: (200, {"format": "someone/v9"}))).run())
    assert result["failed"] == ["billing"]
    assert objects.blobs == {}


# ── an unverified backup is not a backup (Phase 46.4) ─────────────────────────
def test_a_backup_whose_verify_is_unreachable_is_UNVERIFIED_not_ok():
    """Stored, but we could not prove it restores this run — not OK, not lost. Retried next run."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")

    def verify(url, payload):
        raise internal_http.TargetUnreachable("verify endpoint down")

    result = run(_orch(st, objects, _fleet(
        lambda url, p: (200, _sealed_archive("billing")), verify=verify)).run())
    assert result["unverified"] == ["billing"]
    assert result["status"] == "unverified"
    assert result["ok"] == 0 and result["verified"] == 0
    # The archive IS stored — verification is retried, not re-captured from scratch.
    assert len(objects.blobs) == 1


def test_a_backup_that_restores_WRONG_is_FAILED():
    """The verify test-restore ran and found the archive does not restore to an identical copy.
    That is worse than unproven — we KNOW it is not a faithful backup."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")

    def verify(url, payload):
        return (200, {"verified": False, "checked": 1,
                      "mismatches": ["widgets/w1: restored row differs from the archived row"]})

    result = run(_orch(st, objects, _fleet(
        lambda url, p: (200, _sealed_archive("billing")), verify=verify)).run())
    assert result["failed"] == ["billing"] and result["status"] == "failed"


def test_a_verify_rejection_400_is_FAILED():
    """The target refused the stored archive as unrestorable (bad checksum at rest, or key rotated)."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")

    def verify(url, payload):
        return (400, {"detail": "archive checksum does not match its body — corrupt or truncated"})

    result = run(_orch(st, objects, _fleet(
        lambda url, p: (200, _sealed_archive("billing")), verify=verify)).run())
    assert result["failed"] == ["billing"] and result["status"] == "failed"


# ── status is the worst target's ──────────────────────────────────────────────
def test_the_run_status_is_the_worst_target():
    """One failure among successes must not be averaged away into 'mostly fine'."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "ok1", "ok2", "bad")

    def export(url, payload):
        if "bad" in url:
            a = _sealed_archive("bad"); a["checksum"] = "sha256:0"
            return (200, a)
        return (200, _sealed_archive("ok"))

    result = run(_orch(st, objects, _fleet(export)).run())
    assert result["status"] == "failed"        # failed outranks the two oks
    assert result["ok"] == 2 and result["failed"] == ["bad"]


# ── the run is recorded so an alert can read it ───────────────────────────────
def test_the_run_is_recorded():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    result = run(_orch(st, objects, _fleet(lambda url, p: (200, _sealed_archive("billing")))).run())
    stored = run(RunStore(st).get(result["id"]))
    assert stored is not None and stored["status"] == "complete"


def test_a_disabled_target_is_not_captured():
    st, objects = MemoryDataStore(), _Objects()
    ts = TargetStore(st)
    run(ts.upsert(Target(service="paused", base_url="https://x.internal", enabled=False)))
    result = run(_orch(st, objects, _fleet(lambda url, p: (200, _sealed_archive()))).run())
    assert result["targets"] == 0


# ── the target registry never stores the token ────────────────────────────────
def test_a_target_row_exposes_the_token_ref_never_a_token():
    st = MemoryDataStore()
    ts = TargetStore(st)
    run(ts.upsert(Target(service="billing", base_url="https://b.internal", token_ref="billing_tok")))
    pub = run(ts.get("billing")).public()
    assert pub["token_ref"] == "billing_tok"
    assert "token" not in pub or pub.get("token") is None
