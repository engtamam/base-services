"""Regression tests for the 2026-09-12 fleet-audit findings on webhook-service."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.signer.hmac_sha256 import HmacSha256Signer
from app.adapters.transport.httpx_transport import _pin_url
from app.core import signing
from app.core.models import DELIVERED, QUEUED
from app.core.stores import DeliveryStore, EndpointStore, EventStore, InboundStore
from app.core.webhook_service import WebhookService, classify
from app.core.inbound_service import InboundService
from app.ports.webhook_transport import Response

CFG = dict(require_https=False, allow_private_ips=True, allowed_hosts=frozenset(),
           denied_hosts=frozenset(), max_payload_bytes=1_000_000, timeout_seconds=5,
           max_response_bytes=10_000, retry_base_seconds=1, retry_max_seconds=60,
           retry_max_attempts=8, retry_jitter=0.0, circuit_threshold=5, circuit_reset_seconds=300,
           secret_rotation_overlap_seconds=0)


def run(c):
    return asyncio.run(c)


class Transport:
    driver = "t"

    def __init__(self, status=200, ok=True):
        self.status, self.ok, self.sent = status, ok, []

    def send(self, **k):
        self.sent.append(k)
        return Response(ok=self.ok, status=self.status, body="ok", latency_ms=1)


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, e, p):
        self.events.append((e, p))


def _svc(st, transport, bus=None):
    return WebhookService(endpoints=EndpointStore(st, MemoryCache(), 300), events=EventStore(st),
                          deliveries=DeliveryStore(st), transport=transport,
                          signer=HmacSha256Signer(), config=CFG, event_bus=bus)


# ── 05: 3xx is terminal ───────────────────────────────────────────────────────
def test_3xx_is_terminal_not_retryable():
    for s in (301, 302, 307, 308):
        assert classify(s, True) == "terminal"


# ── 02: IPv6 pin builds a valid bracketed authority ───────────────────────────
def test_pin_url_brackets_ipv6_and_keeps_port_and_query():
    assert _pin_url("https://example.com/hook", "2001:db8::1") == "https://[2001:db8::1]/hook"
    assert _pin_url("https://ex.com:8443/h?q=1", "10.0.0.5") == "https://10.0.0.5:8443/h?q=1"
    # a plain IPv4 host is unchanged in shape
    assert _pin_url("http://ex.com/x", "1.2.3.4") == "http://1.2.3.4/x"


# ── 01: a disabled endpoint's delivery is pushed OUT of the due window ─────────
def test_disabled_endpoint_delivery_is_rescheduled_not_permanently_due():
    st = MemoryDataStore(); w = _svc(st, Transport())
    ep = run(w.register(tenant="t1", url="http://127.0.0.1:9/h", event_types=["a"]))
    r = run(w.dispatch(tenant="t1", event_type="a", payload={}))
    did = r["deliveries"][0]
    run(w.update(ep.id, enabled=False))
    res = run(w.deliver(did))
    assert res["reason"] == "endpoint_disabled"
    d = run(st.get("deliveries", did))
    import time
    assert d["status"] == QUEUED and float(d["next_retry_at"]) > time.time() + 1   # deferred


# ── 03: concurrent deliver of one row sends exactly once ──────────────────────
def test_concurrent_deliver_sends_once():
    st = MemoryDataStore(); t = Transport()
    w = _svc(st, t)
    run(w.register(tenant="t1", url="http://127.0.0.1:9/h", event_types=["a"]))
    did = run(w.dispatch(tenant="t1", event_type="a", payload={}))["deliveries"][0]

    async def _race():
        return await asyncio.gather(w.deliver(did), w.deliver(did))

    run(_race())
    assert len(t.sent) == 1                      # the CAS + in-progress guard prevented a double send


# ── 04: concurrent inbound of the same event publishes once ───────────────────
def _inbound(st, bus):
    return InboundService(store=InboundStore(st), signer=HmacSha256Signer(),
                          secrets={"stripe": ["sec"]}, tolerance_seconds=300, event_bus=bus,
                          dedupe_ttl_seconds=3600, max_payload_bytes=1_000_000)


def test_concurrent_duplicate_inbound_publishes_once():
    st = MemoryDataStore(); bus = Bus()
    svc = _inbound(st, bus)
    body = b'{"id":"evt_1"}'
    header = signing.sign(body, "sec", 1000)

    async def _race():
        return await asyncio.gather(
            svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                       payload={"id": "evt_1"}, now=1000),
            svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                       payload={"id": "evt_1"}, now=1000),
        )

    results = run(_race())
    dups = [r.get("duplicate") for r in results]
    assert dups.count(False) == 1 and dups.count(True) == 1   # exactly one first-seen
    assert len(bus.events) == 1                                # published once
