"""Capturing a backup run across registered targets.

The load-bearing test is that a target which could not be reached is recorded as MISSED and makes
the run INCOMPLETE — never silently skipped. "We backed up" while one service was absent is the
exact lie the phase exists to kill. The rest guard the store: never keep an archive a restore would
reject, and never abort the run because one target failed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from app.adapters.datastore.memory import MemoryDataStore
from app.core import internal_http
from app.core.orchestrator import FAILED, MISSED, OK, Orchestrator
from app.core.runs import RunStore
from app.core.targets import Target, TargetStore


def run(c):
    return asyncio.run(c)


class _Objects:
    def __init__(self):
        self.blobs = {}

    async def put(self, key, data, content_type=None):
        self.blobs[key] = data


def _sealed_archive(service="svc", counts=None):
    sealed = "v1:gcm:" + service
    return {"format": "base-backup/v1", "service": service, "taken_at": "2026-07-22T00:00:00Z",
            "tenant": "", "row_counts": counts or {"widgets": 3}, "sealed": sealed,
            "checksum": "sha256:" + hashlib.sha256(sealed.encode()).hexdigest()}


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


# ── the run captures what it can ──────────────────────────────────────────────
def test_a_run_captures_every_target_and_stores_its_archive():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing", "wallet")
    orch = _orch(st, objects, lambda url, p: (200, _sealed_archive("billing")))
    result = run(orch.run())
    assert result["status"] == "complete"
    assert result["ok"] == 2
    assert len(objects.blobs) == 2


# ── the rule that makes a run honest ──────────────────────────────────────────
def test_an_unreachable_target_is_MISSED_not_skipped():
    """The whole point. A run must not report complete while a service was silently absent."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing", "ghost")

    def responder(url, payload):
        if "ghost" in url:
            raise internal_http.TargetUnreachable("connection refused")
        return (200, _sealed_archive("billing"))

    result = run(_orch(st, objects, responder).run())
    assert result["status"] == "incomplete", "a missed target did not make the run incomplete"
    assert result["missed"] == ["ghost"]
    assert result["ok"] == 1
    # The reachable one was still captured — a miss does not abort the run.
    assert any("billing" in k for k in objects.blobs)


def test_a_target_with_no_backup_key_is_missed_not_failed():
    """A 503 means the target refused to export in the clear — correct on its side, a miss on ours."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "nokey")
    result = run(_orch(st, objects, lambda url, p: (503, {})).run())
    assert result["missed"] == ["nokey"] and result["status"] == "incomplete"


# ── never store an archive a restore would reject ─────────────────────────────
def test_a_corrupt_archive_is_FAILED_and_not_stored():
    """The store must never hold something a restore would reject — that is how 'we have backups'
    becomes false at the worst moment."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    bad = _sealed_archive("billing")
    bad["checksum"] = "sha256:deadbeef"
    result = run(_orch(st, objects, lambda url, p: (200, bad)).run())
    assert result["failed"] == ["billing"] and result["status"] == "failed"
    assert objects.blobs == {}, "a corrupt archive was stored anyway"


def test_a_foreign_format_is_failed():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    result = run(_orch(st, objects, lambda url, p: (200, {"format": "someone/v9"})).run())
    assert result["failed"] == ["billing"]
    assert objects.blobs == {}


# ── status is the worst target's ──────────────────────────────────────────────
def test_the_run_status_is_the_worst_target():
    """One failure among successes must not be averaged away into 'mostly fine'."""
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "ok1", "ok2", "bad")

    def responder(url, payload):
        if "bad" in url:
            a = _sealed_archive("bad"); a["checksum"] = "sha256:0"
            return (200, a)
        return (200, _sealed_archive("ok"))

    result = run(_orch(st, objects, responder).run())
    assert result["status"] == "failed"        # failed outranks the two oks
    assert result["ok"] == 2 and result["failed"] == ["bad"]


# ── the run is recorded so an alert can read it ───────────────────────────────
def test_the_run_is_recorded():
    st, objects = MemoryDataStore(), _Objects()
    _register(st, "billing")
    result = run(_orch(st, objects, lambda url, p: (200, _sealed_archive("billing"))).run())
    stored = run(RunStore(st).get(result["id"]))
    assert stored is not None and stored["status"] == "complete"


def test_a_disabled_target_is_not_captured():
    st, objects = MemoryDataStore(), _Objects()
    ts = TargetStore(st)
    run(ts.upsert(Target(service="paused", base_url="https://x.internal", enabled=False)))
    result = run(_orch(st, objects, lambda url, p: (200, _sealed_archive())).run())
    assert result["targets"] == 0


# ── the target registry never stores the token ────────────────────────────────
def test_a_target_row_exposes_the_token_ref_never_a_token():
    st = MemoryDataStore()
    ts = TargetStore(st)
    run(ts.upsert(Target(service="billing", base_url="https://b.internal", token_ref="billing_tok")))
    pub = run(ts.get("billing")).public()
    assert pub["token_ref"] == "billing_tok"
    assert "token" not in pub or pub.get("token") is None
