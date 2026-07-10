"""Unit tests: chaining, tamper/deletion detection, exporters, filters, purge."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.exporter.csv_exporter import CsvExporter
from app.adapters.exporter.json_exporter import JsonExporter
from app.adapters.exporter.ndjson_exporter import NdjsonExporter
from app.adapters.integrity.hash_chain import HashChainIntegrity
from app.adapters.integrity.hmac_chain import HmacChainIntegrity
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService


def run(c):
    return asyncio.run(c)


def _svc(store=None):
    store = store or MemoryDataStore()
    exporters = {"json": JsonExporter(), "csv": CsvExporter(), "ndjson": NdjsonExporter()}
    svc = AuditService(repo=AuditRepository(store), integrity=HashChainIntegrity(),
                       exporters=exporters, default_format="json",
                       partition_mode="global", query_limit=100)
    return svc, store


def test_monotonic_seq_and_chain():
    svc, _ = _svc()
    e1 = run(svc.record(service="identity", action="login", actor="a"))
    e2 = run(svc.record(service="identity", action="logout", actor="a"))
    assert e1.seq == 1 and e2.seq == 2
    assert e2.prev_hash == e1.hash and e1.hash != e2.hash
    assert run(svc.verify())["ok"] is True


def test_tamper_detected():
    svc, store = _svc()
    run(svc.record(service="s", action="a1", actor="x"))
    run(svc.record(service="s", action="a2", actor="x"))
    # mutate the first stored event's content directly
    repo = AuditRepository(store)
    rows = run(repo.iterate("global"))
    victim = rows[0]
    victim["action"] = "TAMPERED"
    run(repo.replace(victim))
    result = run(svc.verify())
    assert result["ok"] is False and result["first_bad_seq"] == 1


def test_deletion_detected():
    svc, store = _svc()
    run(svc.record(service="s", action="a1"))
    run(svc.record(service="s", action="a2"))
    run(svc.record(service="s", action="a3"))
    repo = AuditRepository(store)
    rows = run(repo.iterate("global"))
    run(repo.delete(rows[1]["id"]))  # delete the middle event -> chain breaks
    assert run(svc.verify())["ok"] is False


def test_hmac_chain_deterministic_with_key():
    a = HmacChainIntegrity("k" * 20)
    b = HmacChainIntegrity("k" * 20)
    ev = {"seq": 1, "action": "x", "prev_hash": ""}
    assert a.seal("", ev) == b.seal("", ev)
    assert HmacChainIntegrity("other-key-1234567").seal("", ev) != a.seal("", ev)


def test_exporters():
    svc, _ = _svc()
    run(svc.record(service="s", action="a", metadata={"k": "v"}))
    for fmt, needle in (("json", b"["), ("ndjson", b"action"), ("csv", b"seq,ts")):
        ct, body = run(svc.export({}, fmt))
        assert needle in body


def test_query_filters():
    svc, _ = _svc()
    run(svc.record(service="s", action="login", actor="alice"))
    run(svc.record(service="s", action="logout", actor="bob"))
    assert len(run(svc.query({"actor": "alice"}))) == 1
    assert len(run(svc.query({"action": "logout"}))) == 1


def test_purge_none_keeps_chain():
    svc, _ = _svc()
    for i in range(3):
        run(svc.record(service="s", action=f"a{i}"))
    res = run(svc.purge("2000-01-01T00:00:00+00:00"))  # nothing older
    assert res["purged"] == 0
    assert run(svc.verify())["ok"] is True


def test_purge_all_reanchors():
    svc, _ = _svc()
    for i in range(3):
        run(svc.record(service="s", action=f"a{i}"))
    res = run(svc.purge("2999-01-01T00:00:00+00:00"))  # everything older
    assert res["purged"] == 3
    # a purge marker remains and the chain re-anchors -> still verifies
    assert run(svc.verify())["ok"] is True
