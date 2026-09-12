"""Regression tests for the 2026-09-12 fleet-audit findings on audit-service (#04, #05, #07)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.exporter.csv_exporter import CsvExporter
from app.adapters.integrity.hash_chain import HashChainIntegrity
from app.backup_manifest import BACKUP
from app.core import backup
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService
from app.core.erasure import AuditErasure


def run(c):
    return asyncio.run(c)


# ── 04: CSV formula injection ─────────────────────────────────────────────────
def test_csv_neutralizes_formula_injection():
    ct, body = CsvExporter().export([{
        "seq": 1, "service": "=cmd()", "action": "+2", "resource": "-1", "tenant": "@x",
        "metadata": {"k": "=HYPERLINK(1)"}}])
    text = body.decode()
    assert ct == "text/csv"
    # every attacker-influenced cell that started with a trigger is prefixed with a quote
    assert "'=cmd()" in text and "'+2" in text and "'-1" in text and "'@x" in text
    assert "'=cmd()" in text  # sanity
    assert "=cmd()," not in text.replace("'=cmd()", "")   # no un-neutralised leading =


# ── 05: restore-as-clone must NOT rewrite tenant on the (non-tenant-scoped) chain ─
def test_clone_restore_does_not_break_the_audit_chain():
    seal, unseal = backup.make_sealer("audit-kek-123")
    src = MemoryDataStore()
    svc = AuditService(repo=AuditRepository(src), integrity=HashChainIntegrity(), exporters={},
                       default_format="json", partition_mode="global", query_limit=100)
    run(svc.record(service="identity", action="login", actor="a", tenant="acme"))
    run(svc.record(service="identity", action="logout", actor="a", tenant="acme"))
    arch = run(backup.export(src, BACKUP, service="audit", version="1", taken_at="t",
                             seal=seal, tenant="", scan_cap=1000))
    dst = MemoryDataStore()
    run(backup.import_archive(dst, arch, unseal=unseal, into_tenant="beta", declaration=BACKUP))
    # the cloned chain must still verify — tenant (part of the hashed payload) was NOT rewritten
    dsvc = AuditService(repo=AuditRepository(dst), integrity=HashChainIntegrity(), exporters={},
                        default_format="json", partition_mode="global", query_limit=100)
    assert run(dsvc.verify())["ok"] is True
    rows = run(AuditRepository(dst).iterate("global"))
    assert all(r["tenant"] == "acme" for r in rows)   # original tenant preserved (chain intact)


# ── 07: erasure verifies the SUBJECT's partition in tenant mode ───────────────
def test_erasure_verifies_the_tenant_partition_in_tenant_mode():
    store = MemoryDataStore()
    svc = AuditService(repo=AuditRepository(store), integrity=HashChainIntegrity(), exporters={},
                       default_format="json", partition_mode="tenant", query_limit=100)
    run(svc.record(service="s", action="a", actor="x", tenant="acme"))
    ack = run(AuditErasure(audit=svc).erase("acme", "sub_ref"))
    # the acme partition has a real 1-event chain; verifying it is meaningful and intact
    assert ack["chain_verifies"] is True
    # prove it checked acme's partition, not the empty 'global' one
    assert run(svc.verify("acme"))["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Second round (2026-09-12): findings 01, 03, 06 — chain-append atomicity,
# keyed integrity by default, atomic stale-idempotency take-over.
# ─────────────────────────────────────────────────────────────────────────────
import pytest  # noqa: E402

from app.adapters.exporter.json_exporter import JsonExporter  # noqa: E402
from app.adapters.integrity.hmac_chain import HmacChainIntegrity  # noqa: E402


class _YieldStore(MemoryDataStore):
    """Yields the event loop inside get(), so two coroutines racing on the same key actually
    interleave under asyncio.gather (a plain MemoryDataStore never awaits internally)."""

    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _svc2(store):
    return AuditService(repo=AuditRepository(store), integrity=HashChainIntegrity(),
                        exporters={"json": JsonExporter()}, default_format="json",
                        partition_mode="global", query_limit=100)


# ── 01: concurrent record() calls produce a valid, gapless chain ──────────────
def test_concurrent_records_keep_the_chain_intact():
    store = _YieldStore()
    svc = _svc2(store)

    async def _race():
        return await asyncio.gather(*[
            svc.record(service="s", action=f"a{i}", actor="x") for i in range(8)])

    evts = run(_race())
    seqs = sorted(e.seq for e in evts)
    assert seqs == list(range(1, 9))                 # no duplicate/missing seq (pre-fix: dup seqs)
    assert run(svc.verify())["ok"] is True           # prev_hash chain unbroken (pre-fix: tamper alarm)


# ── 03: the integrity chain is HMAC-keyed by default, and fails closed w/o a key ─
def test_default_integrity_driver_is_keyed():
    from app.config import Settings
    assert Settings.model_fields["INTEGRITY_DRIVER"].default == "hmac-chain"


def test_hmac_chain_requires_a_key():
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        HmacChainIntegrity("")                       # fail closed: no unkeyed default


def test_keyed_chain_resists_recompute_without_the_key():
    # An attacker who edits the store but lacks the key cannot forge a chain that verifies.
    store = MemoryDataStore()
    svc = AuditService(repo=AuditRepository(store), integrity=HmacChainIntegrity("k" * 20),
                       exporters={}, default_format="json", partition_mode="global", query_limit=100)
    run(svc.record(service="s", action="a1", actor="x"))
    run(svc.record(service="s", action="a2", actor="x"))
    # re-seal every event with a PUBLIC sha256 (what a backend attacker can compute) and re-anchor
    forger = HashChainIntegrity()
    repo = AuditRepository(store)
    rows = run(repo.iterate("global"))
    prev = ""
    for r in rows:
        r["action"] = "tampered"
        r["prev_hash"] = prev
        r["hash"] = forger.seal(prev, r)
        run(repo.replace(r))
        prev = r["hash"]
    run(repo.set_head_versioned("global", run(repo.head_versioned("global"))[2], rows[-1]["seq"], prev))
    assert run(svc.verify())["ok"] is False          # HMAC verify rejects the sha256-forged chain


# ── 06: two concurrent stale take-overs append exactly one event ──────────────
def test_concurrent_stale_takeover_appends_once():
    store = _YieldStore()
    svc = _svc2(store)
    # seed a STALE pending reservation for the key (ts far in the past)
    ns = "1:s|0:|evt-1"
    run(store.cas("audit_idempo", ns, 0, {"key": ns, "state": "pending", "ts": 0.0}))

    async def _race():
        return await asyncio.gather(
            svc.record_idempotent(idempotency_key="evt-1", stale_seconds=1.0,
                                  service="s", action="a", actor="x"),
            svc.record_idempotent(idempotency_key="evt-1", stale_seconds=1.0,
                                  service="s", action="a", actor="x"),
            return_exceptions=True)

    run(_race())
    rows = run(AuditRepository(store).iterate("global"))
    assert len(rows) == 1                             # exactly one event (pre-fix: two, chain forked)


# ─────────────────────────────────────────────────────────────────────────────
# Round 2 (2026-09-12): findings r2-01, r2-03, r2-04, r2-07 — purge/append mutual
# exclusion, idempotency crash-window duplicate, integrity fail-open, verify()
# spurious break. Each test is RED on the pre-fix code.
# ─────────────────────────────────────────────────────────────────────────────
import datetime as _dt  # noqa: E402


def _old_ts(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()


class _AppendDuringIterate(MemoryDataStore):
    """Fires a hook exactly once, AFTER an audit_events snapshot is taken but BEFORE it is returned.

    This lands a concurrent append strictly after purge's iterate() read — the precise interleaving
    that orphans the appended event and breaks the chain on the pre-fix purge (audit-r2-01)."""

    def __init__(self) -> None:
        super().__init__()
        self._hook = None
        self._fired = False

    async def find(self, collection, where, **kw):
        rows, cur = await super().find(collection, where, **kw)
        if collection == "audit_events" and self._hook is not None and not self._fired:
            self._fired = True
            await self._hook()
        return rows, cur


def test_purge_is_serialised_against_a_concurrent_append():
    store = _AppendDuringIterate()
    svc = AuditService(repo=AuditRepository(store), integrity=HmacChainIntegrity("k" * 20),
                       exporters={"json": JsonExporter()}, default_format="json",
                       partition_mode="global", query_limit=100)
    # one OLD (expired) event + one RECENT (kept) event
    ev_old = run(svc.record(service="s", action="old", actor="x"))
    row = run(store.get("audit_events", ev_old.id))
    row["ts"] = _old_ts(400)
    run(store.put("audit_events", ev_old.id, row))
    run(svc.record(service="s", action="recent", actor="x"))

    # A concurrent append lands right after purge snapshots the chain (pre-fix: orphaned + head
    # rolled back below it -> verify() reports a real event as tampering).
    store._hook = lambda: svc.record(service="s", action="concurrent", actor="x")
    run(svc.purge(_old_ts(200), "global"))

    assert run(svc.verify("global"))["ok"] is True         # chain intact (pre-fix: broken)
    actions = [r["action"] for r in run(AuditRepository(store).iterate("global"))]
    assert "concurrent" in actions                         # not orphaned/lost
    assert "old" not in actions                            # expired prefix purged


def test_stale_takeover_adopts_an_already_appended_event():
    # Crash after append but before idempo_commit: the key stays `pending` while the event is
    # already in the chain. A stale take-over must adopt it, not append a duplicate (audit-r2-03).
    store = MemoryDataStore()
    svc = _svc2(store)
    ns = "1:s|0:|evt-1"
    ev = run(svc.record(idempo_key=ns, service="s", action="a", actor="x"))   # reserver appended…
    run(store.cas("audit_idempo", ns, 0, {"key": ns, "state": "pending", "ts": 0.0}))  # …then crashed

    receipt, duplicate = run(svc.record_idempotent(
        idempotency_key="evt-1", stale_seconds=1.0, service="s", action="a", actor="x"))

    rows = run(AuditRepository(store).iterate("global"))
    assert len(rows) == 1                                   # no duplicate (pre-fix: two)
    assert duplicate is True and receipt["seq"] == ev.seq   # adopted the existing receipt


class _TailSettles(MemoryDataStore):
    """The FIRST audit_events snapshot hides the newest row (head is already advanced, but the just
    -written event row is not yet visible to this reader); later snapshots reveal it. A deterministic
    stand-in for an in-flight append that settles across verify()'s re-read (audit-r2-07)."""

    def __init__(self) -> None:
        super().__init__()
        self.hide_newest_once = False
        self._hidden = False

    async def find(self, collection, where, **kw):
        rows, cur = await super().find(collection, where, **kw)
        if collection == "audit_events" and self.hide_newest_once and not self._hidden and rows:
            self._hidden = True
            rows = rows[:-1]                 # tail appears one short of the recorded head
        return rows, cur


def test_verify_does_not_cry_broken_on_an_in_flight_append():
    store = _TailSettles()
    svc = _svc2(store)
    run(svc.record(service="s", action="a1", actor="x"))
    run(svc.record(service="s", action="a2", actor="x"))
    run(svc.record(service="s", action="a3", actor="x"))

    store.hide_newest_once = True           # first snapshot: head at 3 but only a1,a2 visible
    assert run(svc.verify("global"))["ok"] is True    # reconciled on re-read (pre-fix: broken)


def test_verify_still_reports_a_truly_truncated_tail():
    # The reconcile must not MASK a real break: a stable head ahead of the tail is tamper.
    store = MemoryDataStore()
    svc = _svc2(store)
    run(svc.record(service="s", action="a1", actor="x"))
    ev2 = run(svc.record(service="s", action="a2", actor="x"))
    run(store.delete("audit_events", ev2.id))     # delete the newest event, leave head at seq 2
    out = run(svc.verify("global"))
    assert out["ok"] is False


def _prod_settings(**overrides):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="a" * 40, ENV="production",
                PII_SALT="frozen-salt", AUDIT_SINK_DRIVER="http",
                AUDIT_SERVICE_URL="http://audit.internal")
    base.update(overrides)
    return Settings(**base)


def test_integrity_none_refused_in_production():
    with pytest.raises(ValueError):
        _prod_settings(INTEGRITY_DRIVER="none").assert_audit_config_ok()


def test_integrity_unkeyed_hash_chain_refused_in_production_by_default():
    with pytest.raises(ValueError):
        _prod_settings(INTEGRITY_DRIVER="hash-chain").assert_audit_config_ok()


def test_integrity_unkeyed_hash_chain_allowed_with_explicit_optout():
    _prod_settings(INTEGRITY_DRIVER="hash-chain",
                   INTEGRITY_ALLOW_UNKEYED=True).assert_audit_config_ok()  # no raise


def test_integrity_hmac_chain_is_fine_in_production():
    _prod_settings(INTEGRITY_DRIVER="hmac-chain").assert_audit_config_ok()  # no raise
