"""Inbound: signature = auth, replay protection, dedupe on the PROVIDER's event id."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.signer.hmac_sha256 import HmacSha256Signer
from app.adapters.signer.null import NullSigner
from app.core import signing
from app.core.inbound_service import InboundRejected, InboundService
from app.core.stores import InboundStore


def run(c):
    return asyncio.run(c)


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, e, p):
        self.events.append((e, p))


def _svc(st=None, signer=None, secrets=None, bus=None, tolerance=300):
    return InboundService(store=InboundStore(st or MemoryDataStore()), signer=signer or HmacSha256Signer(),
                          secrets=secrets if secrets is not None else {"stripe": ["sec"]},
                          tolerance_seconds=tolerance, event_bus=bus)


def _sign(body, secret="sec", ts=1000):
    return signing.sign(body, secret, ts)


def test_valid_signature_accepted_and_published():
    bus = Bus()
    svc = _svc(bus=bus)
    body = b'{"id":"evt_1"}'
    out = run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="evt_1",
                         payload={"id": "evt_1"}, now=1000))
    assert out["accepted"] and not out["duplicate"]
    assert bus.events[0][0] == "inbound.stripe"


def test_invalid_signature_rejected_and_not_stored():
    st = MemoryDataStore()
    svc = _svc(st)
    body = b'{"id":"evt_1"}'
    with pytest.raises(InboundRejected, match="INVALID_SIGNATURE"):
        run(svc.ingest(source="stripe", body=body, signature=_sign(body, secret="wrong"),
                       provider_event_id="evt_1", payload={}, now=1000))
    assert run(InboundStore(st).seen("stripe", "evt_1")) is False    # never persisted


def test_replayed_old_payload_rejected():
    svc = _svc(tolerance=300)
    body = b'{"id":"evt_1"}'
    header = _sign(body, ts=1000)
    with pytest.raises(InboundRejected, match="INVALID_SIGNATURE"):
        run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                       payload={}, now=1000 + 3600))                 # an hour later


def test_unknown_source_is_fail_closed():
    svc = _svc(secrets={})
    body = b'{"id":"e"}'
    with pytest.raises(InboundRejected, match="UNKNOWN_SOURCE"):
        run(svc.ingest(source="mystery", body=body, signature=_sign(body), provider_event_id="e",
                       payload={}, now=1000))


def test_null_signer_never_accepts():
    svc = _svc(signer=NullSigner())
    body = b'{"id":"e"}'
    with pytest.raises(InboundRejected, match="INVALID_SIGNATURE"):
        run(svc.ingest(source="stripe", body=body, signature="anything", provider_event_id="e",
                       payload={}, now=1000))


def test_dedupe_on_provider_event_id_not_body_hash():
    """Two DISTINCT events can carry byte-identical bodies — hashing the body would drop one."""
    bus = Bus()
    svc = _svc(bus=bus)
    body = b'{"type":"ping"}'                     # identical bodies
    header = _sign(body)
    a = run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                       payload={}, now=1000))
    b = run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_2",
                       payload={}, now=1000))
    assert a["duplicate"] is False and b["duplicate"] is False       # both accepted
    dup = run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                         payload={}, now=1000))
    assert dup["duplicate"] is True
    assert len(bus.events) == 2                                       # the duplicate is not republished


def test_same_provider_id_from_different_sources_are_distinct():
    svc = _svc(secrets={"stripe": ["sec"], "github": ["sec"]})
    body = b'{"id":"1"}'
    header = _sign(body)
    assert run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="1",
                          payload={}, now=1000))["duplicate"] is False
    assert run(svc.ingest(source="github", body=body, signature=header, provider_event_id="1",
                          payload={}, now=1000))["duplicate"] is False


def test_missing_event_id_rejected():
    svc = _svc()
    body = b'{}'
    with pytest.raises(InboundRejected, match="MISSING_EVENT_ID"):
        run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="",
                       payload={}, now=1000))


def test_oversized_payload_rejected():
    svc = InboundService(store=InboundStore(MemoryDataStore()), signer=HmacSha256Signer(),
                         secrets={"stripe": ["sec"]}, max_payload_bytes=10)
    with pytest.raises(InboundRejected, match="PAYLOAD_TOO_LARGE"):
        run(svc.ingest(source="stripe", body=b"x" * 100, signature="", provider_event_id="e",
                       payload={}, now=1000))


def test_secret_rotation_accepts_the_old_secret():
    svc = _svc(secrets={"stripe": ["new", "old"]})
    body = b'{"id":"e"}'
    header = _sign(body, secret="old")
    assert run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="e",
                          payload={}, now=1000))["accepted"]


def test_dedupe_window_expires_so_records_do_not_grow_forever():
    st = MemoryDataStore()
    svc = InboundService(store=InboundStore(st), signer=HmacSha256Signer(),
                         secrets={"stripe": ["sec"]}, tolerance_seconds=10**9,
                         dedupe_ttl_seconds=3600)
    body = b'{"id":"evt_1"}'
    run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="evt_1",
                   payload={}, now=1000))
    within = run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="evt_1",
                            payload={}, now=1000 + 60))
    assert within["duplicate"] is True                     # inside the window → suppressed
    after = run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="evt_1",
                           payload={}, now=1000 + 7200))
    assert after["duplicate"] is False                     # window elapsed → treated as new


def test_bus_outage_never_fails_ingest():
    class BadBus:
        async def publish(self, *a, **k):
            raise RuntimeError("bus down")

    svc = _svc(bus=BadBus())
    body = b'{"id":"e"}'
    assert run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="e",
                          payload={}, now=1000))["accepted"]


# ── webhook-002: a bus outage leaves the event PENDING; the sweep re-publishes it ────────────────────
class FlakyBus:
    def __init__(self, fail=True):
        self.fail, self.events = fail, []

    async def publish(self, e, p):
        if self.fail:
            raise RuntimeError("bus down")
        self.events.append((e, p))


def test_bus_outage_marks_pending_then_sweep_republishes():
    st = MemoryDataStore()
    bus = FlakyBus(fail=True)
    svc = _svc(st, bus=bus)
    body = b'{"id":"evt_9"}'
    out = run(svc.ingest(source="stripe", body=body, signature=_sign(body), provider_event_id="evt_9",
                         payload={"id": "evt_9"}, now=1000))
    assert out["accepted"] and not out["duplicate"]          # provider still gets a clean 200
    row = run(st.get("inbound", "stripe::evt_9"))
    assert row["published"] is False                          # stored PENDING, not lost
    # bus recovers → the outbox sweep re-publishes it and clears the flag
    bus.fail = False
    res = run(svc.republish_pending())
    assert res["republished"] == 1
    assert bus.events[0][0] == "inbound.stripe"
    assert run(st.get("inbound", "stripe::evt_9"))["published"] is True
    # a second sweep does nothing (idempotent)
    assert run(svc.republish_pending())["republished"] == 0


# ── webhook-003: a GitHub-style sha256=HMAC(body) signature verifies ─────────────────────────────────
def test_github_scheme_signature_verifies():
    import hashlib
    import hmac as _hmac
    body = b'{"zen":"x","hook_id":1}'
    secret = "gh-secret"
    sig = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert signing.verify(body, sig, [secret]) is True
    assert signing.verify(body, sig, ["wrong"]) is False       # wrong secret rejected
    assert signing.verify(b'{"tampered":1}', sig, [secret]) is False  # tampered body rejected
