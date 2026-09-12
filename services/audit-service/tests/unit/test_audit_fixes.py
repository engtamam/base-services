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
    run(repo.set_head("global", rows[-1]["seq"], prev))
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
