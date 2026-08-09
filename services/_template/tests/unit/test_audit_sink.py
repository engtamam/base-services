"""AuditSink adapters. The question every test here asks: can this claim an action was witnessed
when the chain does not actually hold it?

That is the only way an audit log fails dangerously. A log that loses events is visibly broken; a
log that reports success while staying empty passes every compliance questionnaire and answers
nothing in an investigation — which is precisely the state this platform was in.
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest

from app.adapters.audit_sink.eventbus import EventBusAuditSink
from app.adapters.audit_sink.http import HttpAuditSink
from app.adapters.audit_sink.null import NullAuditSink
from app.ports.audit_sink import ACCEPTED, DROPPED, FAILED, AuditEvent, AuditSink

_URL = "http://audit-service:8000/internal/audit"
_TOKEN = "t" * 40
_EVENT = AuditEvent(service="identity-service", action="user.suspended",
                    actor="sub_abc123", resource="user:u_7", tenant="acme", ip="10.0.0.1",
                    metadata={"reason": "abuse"})
_RECEIPT = {"id": "evt_1", "seq": 42, "hash": "h_deadbeef"}


def run(c):
    return asyncio.run(c)


class _Resp(io.BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(monkeypatch, resp):
    seen = {}

    def fake(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr("app.adapters.audit_sink.http.urllib.request.urlopen", fake)
    return seen


def test_every_adapter_satisfies_the_port():
    assert isinstance(NullAuditSink(), AuditSink)
    assert isinstance(HttpAuditSink(_URL, _TOKEN), AuditSink)
    assert isinstance(EventBusAuditSink(None, "audit.events"), AuditSink)


# ── null ──────────────────────────────────────────────────────────────────────
def test_null_never_claims_the_event_was_recorded():
    # THE test for this adapter. A null sink returning ACCEPTED would mean every service reported
    # "audited" while the chain stayed empty — actively lying rather than merely disconnected.
    sink = NullAuditSink()
    receipt = run(sink.emit(_EVENT))
    assert receipt.status == DROPPED
    assert receipt.is_recorded is False
    assert "NOT recorded" in receipt.detail
    assert sink.events == [_EVENT]     # it records, so a test can prove the sink was REACHED


# ── http ──────────────────────────────────────────────────────────────────────
def test_http_accepted_carries_the_chain_position(monkeypatch):
    seen = _urlopen(monkeypatch, _Resp(200, json.dumps(_RECEIPT).encode()))
    receipt = run(HttpAuditSink(_URL, _TOKEN, timeout=5).emit(_EVENT))
    assert receipt.is_recorded
    assert (receipt.id, receipt.seq, receipt.hash) == ("evt_1", 42, "h_deadbeef")
    req = seen["req"]
    assert req.method == "POST" and req.full_url == _URL
    assert req.get_header("X-internal-token") == _TOKEN
    assert seen["timeout"] == 5


def test_http_sends_exactly_the_ingest_contract(monkeypatch):
    # A field name drifting from audit-service's AuditIn would 422 every event in production while
    # every unit test here still passed. The idempotency_key is part of that contract now — it is
    # what lets a retried timeout be recognised by the log instead of appended a second time.
    seen = _urlopen(monkeypatch, _Resp(200, json.dumps(_RECEIPT).encode()))
    run(HttpAuditSink(_URL, _TOKEN).emit(_EVENT))
    sent = json.loads(seen["req"].data)
    key = sent.pop("idempotency_key", None)
    assert isinstance(key, str) and key                # present, non-empty
    assert sent == {
        "service": "identity-service", "action": "user.suspended", "actor": "sub_abc123",
        "resource": "user:u_7", "tenant": "acme", "ip": "10.0.0.1",
        "metadata": {"reason": "abuse"}}


def test_the_actor_is_a_pseudonym_not_a_person(monkeypatch):
    # An audit log full of raw identities becomes a PII store that CANNOT honour an erasure,
    # because removing a row from a hash chain is exactly what breaks it.
    seen = _urlopen(monkeypatch, _Resp(200, json.dumps(_RECEIPT).encode()))
    run(HttpAuditSink(_URL, _TOKEN).emit(_EVENT))
    assert json.loads(seen["req"].data)["actor"].startswith("sub_")


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", json.dumps({"id": "x"}).encode()])
def test_a_200_without_a_chain_hash_is_not_proof(monkeypatch, body):
    # The seq and hash ARE the evidence of where the event sits. A 200 with neither proves nothing,
    # and treating it as recorded is how a chain ends up trusted while empty.
    _urlopen(monkeypatch, _Resp(200, body))
    assert run(HttpAuditSink(_URL, _TOKEN).emit(_EVENT)).status == FAILED


@pytest.mark.parametrize("boom", [
    urllib.error.HTTPError("u", 500, "boom", {}, None),
    urllib.error.HTTPError("u", 401, "nope", {}, None),
    urllib.error.URLError("connection refused"),
    OSError("timed out"),
])
def test_http_failures_are_reported_never_raised(monkeypatch, boom):
    # A sink that raised would let a logging hiccup fail the very action it was witnessing —
    # turning a security control into an outage.
    _urlopen(monkeypatch, boom)
    receipt = run(HttpAuditSink(_URL, _TOKEN).emit(_EVENT))
    assert receipt.status == FAILED and receipt.detail


def test_http_retries_a_transient_failure_reusing_one_idempotency_key(monkeypatch):
    """A timeout MAY have already landed the append, so the sink retries — but every attempt carries
    the SAME idempotency_key, so audit-service recognises the repeat and appends nothing. Retrying is
    safe BECAUSE the key is stable; a retry that changed the key would be the very duplicate an audit
    log must never invent. This is the invariant that replaced the old 'never retry' rule."""
    bodies = []

    def fake(req, timeout=None):
        bodies.append(json.loads(req.data))
        raise OSError("timed out")

    monkeypatch.setattr("app.adapters.audit_sink.http.urllib.request.urlopen", fake)
    receipt = run(HttpAuditSink(_URL, _TOKEN, max_attempts=2).emit(_EVENT))
    assert receipt.status == FAILED                    # every attempt failed — reported, never raised
    assert len(bodies) == 2                            # it DID retry the transient failure
    assert len({b["idempotency_key"] for b in bodies}) == 1   # ONE key across retries — no duplicate


# ── eventbus ──────────────────────────────────────────────────────────────────
def test_eventbus_publishes_the_event():
    class _Bus:
        def __init__(self):
            self.published = []

        async def publish(self, topic, payload):
            self.published.append((topic, payload))

    bus = _Bus()
    receipt = run(EventBusAuditSink(bus, "audit.events").emit(_EVENT))
    assert receipt.status == ACCEPTED
    assert bus.published == [("audit.events", _EVENT.to_payload())]


def test_eventbus_does_not_pretend_to_know_the_chain_position():
    # It was accepted by the BUS, not sealed into the chain. Claiming a hash here would assert
    # something no one has verified yet.
    class _Bus:
        async def publish(self, topic, payload):
            return None

    receipt = run(EventBusAuditSink(_Bus(), "audit.events").emit(_EVENT))
    assert receipt.hash == "" and receipt.seq == 0
    assert "not yet sealed" in receipt.detail


def test_a_refusing_bus_is_reported_not_raised():
    class _Bus:
        async def publish(self, topic, payload):
            raise RuntimeError("broker down")

    receipt = run(EventBusAuditSink(_Bus(), "audit.events").emit(_EVENT))
    assert receipt.status == FAILED and "broker down" in receipt.detail
