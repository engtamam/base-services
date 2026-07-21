"""The in-process audit sink — audit-service witnesses itself without leaving the process.

Recording its own actions over HTTP to itself is a real hang: an erase handler holds a worker while
it POSTs subject.erased back to the same service, so under load every worker is busy, the self-call
finds no free loop, times out, and the critical action fail-closes with a 503. This sink appends to
the local chain directly — no socket, no worker, no timeout.
"""
from __future__ import annotations

import asyncio

from app.adapters.audit_sink.in_process import InProcessAuditSink
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.integrity.hash_chain import HashChainIntegrity
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService
from app.ports.audit_sink import ACCEPTED, AuditEvent


class _S:
    AUDIT_PARTITION = "global"
    AUDIT_QUERY_LIMIT = 100
    AUDIT_EXPORT_LIMIT = 100_000
    EXPORTER_DEFAULT = "json"
    AUDIT_PII_MODE = "off"
    AUDIT_PII_FIELDS = ""
    PII_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


def _wire():
    ds = MemoryDataStore()
    integ = HashChainIntegrity()
    sink = InProcessAuditSink(datastore=ds, integrity=integ, settings=_S())
    return ds, integ, sink


def test_emit_appends_to_the_local_chain_and_returns_the_seal():
    ds, integ, sink = _wire()
    r = run(sink.emit(AuditEvent(service="audit-service", action="subject.erased",
                                 actor="sub_x", tenant="acme")))
    assert r.status == ACCEPTED and r.seq == 1 and r.hash


def test_the_event_is_in_the_same_chain_the_service_verifies():
    # The whole point: it records into the chain the rest of the service reads — not a second one.
    ds, integ, sink = _wire()
    run(sink.emit(AuditEvent(service="audit-service", action="audit.verified", tenant="acme")))
    svc = AuditService(repo=AuditRepository(ds), integrity=integ, exporters={},
                       default_format="json", partition_mode="global", query_limit=100)
    assert run(svc.verify())["ok"] is True
    assert run(svc.verify())["checked"] == 1


def test_a_sink_failure_returns_FAILED_and_never_raises():
    # A logging fault must not fail the action it witnesses — same contract as every sink.
    class _Broken:
        driver = "memory"
        async def get(self, *a, **k): raise RuntimeError("store down")
        async def put(self, *a, **k): raise RuntimeError("store down")
        async def find(self, *a, **k): raise RuntimeError("store down")
    sink = InProcessAuditSink(datastore=_Broken(), integrity=HashChainIntegrity(), settings=_S())
    r = run(sink.emit(AuditEvent(service="audit-service", action="x.y")))
    assert r.status != ACCEPTED and "failed" in r.detail.lower()


def test_the_sink_makes_no_http_call():
    # It must not import or use urllib — that is the whole defect it removes.
    import inspect
    import app.adapters.audit_sink.in_process as m
    src = inspect.getsource(m)
    # No network client at all — the comments discuss HTTP to explain why it is absent, so we
    # check for actual USE, not the word.
    for token in ("urllib", "import http", "httpx", "requests.", "urlopen", ".request("):
        assert token not in src, f"in-process sink must not touch the network: found {token!r}"
