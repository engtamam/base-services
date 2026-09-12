"""Non-functional guards (per memory `phase-doc-rules`).

`dispatch()` runs for every internal event and `POST /inbound/{source}` for every
third-party event. Neither may scan the datastore.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.signer.hmac_sha256 import HmacSha256Signer
from app.core import signing
from app.core.inbound_service import InboundService
from app.core.stores import DeliveryStore, EndpointStore, EventStore, InboundStore
from app.core.webhook_service import WebhookService
from app.ports.webhook_transport import Response

CFG = dict(require_https=False, allow_private_ips=True, allowed_hosts=frozenset(),
           denied_hosts=frozenset(), timeout_seconds=5, retry_base_seconds=30,
           retry_max_seconds=3600, retry_max_attempts=3, retry_jitter=True,
           circuit_threshold=5, circuit_reset_seconds=300, max_payload_bytes=1_048_576,
           max_response_bytes=1024, endpoint_concurrency=4, list_limit=100)


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.writes = self.reads = self.scans = 0

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def cas(self, c, k, expected, v):
        ok = await super().cas(c, k, expected, v)
        if ok:
            self.writes += 1      # only a SUCCESSFUL cas wrote; a losing/duplicate cas is a no-op
        return ok

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def scan_all(self, c, limit=100):
        self.scans += 1
        return await super().scan_all(c, limit)


class Transport:
    driver = "t"

    def send(self, **k):
        return Response(ok=True, status=200, latency_ms=1)


def _svc(st, cache):
    return WebhookService(endpoints=EndpointStore(st, cache, 300), events=EventStore(st),
                          deliveries=DeliveryStore(st), transport=Transport(),
                          signer=HmacSha256Signer(), config=CFG)


# ── dispatch: subscriber lookup must be cached ────────────────────────────────
def test_dispatch_never_scans_after_warm():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    for i in range(10):
        run(w.register(tenant="t1", url=f"http://127.0.0.1:900{i % 10}/h", event_types=["user.created"]))
    run(w.dispatch(tenant="t1", event_type="user.created", payload={}))    # warm
    st.scans = st.reads = 0
    for _ in range(50):
        run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    assert st.scans == 0, f"dispatch scanned the endpoint table {st.scans} times"
    assert st.reads == 0, f"dispatch did {st.reads} datastore reads for endpoint lookup"


def test_dispatch_writes_only_event_plus_one_delivery_per_subscriber():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    for i in range(3):
        run(w.register(tenant="t1", url=f"http://127.0.0.1:900{i}/h", event_types=["*"]))
    run(w.dispatch(tenant="t1", event_type="x", payload={}))               # warm
    st.writes = 0
    run(w.dispatch(tenant="t1", event_type="x", payload={}))
    assert st.writes == 1 + 3          # the event + one delivery per subscriber. Nothing else.


def test_endpoint_write_invalidates_the_subscriber_cache():
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    run(w.register(tenant="t1", url="http://127.0.0.1:9001/h", event_types=["a"]))
    assert run(w.dispatch(tenant="t1", event_type="a", payload={}))["subscribers"] == 1
    run(w.register(tenant="t1", url="http://127.0.0.1:9002/h", event_types=["a"]))
    assert run(w.dispatch(tenant="t1", event_type="a", payload={}))["subscribers"] == 2


def test_circuit_and_stat_updates_do_not_bust_the_subscriber_cache():
    """Delivery state changes constantly; it must not invalidate the hot-path index."""
    st, cache = CountingStore(), MemoryCache()
    w = _svc(st, cache)
    ep = run(w.register(tenant="t1", url="http://127.0.0.1:9001/h", event_types=["a"]))
    r = run(w.dispatch(tenant="t1", event_type="a", payload={}))
    run(w.deliver(r["deliveries"][0]))               # writes endpoint stats via save_state
    st.scans = 0
    run(w.dispatch(tenant="t1", event_type="a", payload={}))
    assert st.scans == 0


def test_cache_outage_never_breaks_dispatch():
    class BrokenCache:
        driver = "broken"

        async def get(self, key):
            raise RuntimeError("down")

        async def set(self, key, value, ttl_seconds=None):
            raise RuntimeError("down")

        async def delete(self, key):
            raise RuntimeError("down")

    st = MemoryDataStore()
    w = _svc(st, BrokenCache())
    run(w.register(tenant="t1", url="http://127.0.0.1:9001/h", event_types=["a"]))
    assert run(w.dispatch(tenant="t1", event_type="a", payload={}))["subscribers"] == 1


# ── inbound: exactly one write, no scans ──────────────────────────────────────
def test_inbound_ingest_does_exactly_one_write_and_no_scan():
    st = CountingStore()
    svc = InboundService(store=InboundStore(st), signer=HmacSha256Signer(),
                         secrets={"stripe": ["sec"]}, tolerance_seconds=300)
    body = b'{"id":"evt_1"}'
    header = signing.sign(body, "sec", 1000)
    st.writes = st.scans = 0
    run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                   payload={"id": "evt_1"}, now=1000))
    assert st.writes == 1 and st.scans == 0


def test_inbound_duplicate_does_not_write_again():
    st = CountingStore()
    svc = InboundService(store=InboundStore(st), signer=HmacSha256Signer(),
                         secrets={"stripe": ["sec"]}, tolerance_seconds=300)
    body = b'{"id":"evt_1"}'
    header = signing.sign(body, "sec", 1000)
    run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                   payload={}, now=1000))
    st.writes = 0
    out = run(svc.ingest(source="stripe", body=body, signature=header, provider_event_id="evt_1",
                         payload={}, now=1000))
    assert out["duplicate"] is True and st.writes == 0


# ── every config knob must have a consumer ────────────────────────────────────
def test_every_settings_field_is_consumed_somewhere():
    """An unwired knob is a lie in the API surface (the Phase-16 lesson)."""
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file()
             and p.suffix in (".py", ".toml", "") and "tests" not in p.parts
             and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue

    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
