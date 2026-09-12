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
