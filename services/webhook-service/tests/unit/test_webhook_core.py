"""Outbound core: dispatch, signed delivery, classify, retry, circuit, DLQ, replay, rotation."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.signer.hmac_sha256 import HmacSha256Signer
from app.core import signing
from app.core.models import DEAD, DELIVERED
from app.core.ssrf import SsrfBlocked
from app.core.stores import DeliveryStore, EndpointStore, EventStore
from app.core.webhook_service import WebhookService, classify
from app.ports.webhook_transport import Response

CFG = dict(require_https=False, allow_private_ips=True, allowed_hosts=frozenset(),
           denied_hosts=frozenset(), timeout_seconds=5, retry_base_seconds=30,
           retry_max_seconds=3600, retry_max_attempts=3, retry_jitter=True,
           circuit_threshold=2, circuit_reset_seconds=300, max_payload_bytes=1024,
           max_response_bytes=1024, endpoint_concurrency=4, list_limit=100,
           secret_rotation_overlap_seconds=604_800)


def run(c):
    return asyncio.run(c)


class Transport:
    driver = "t"

    def __init__(self, status=200, ok=True):
        self.status, self.ok, self.sent = status, ok, []

    def send(self, **k):
        self.sent.append(k)
        return Response(ok=self.ok, status=self.status, body="ok", latency_ms=3)


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, e, p):
        self.events.append((e, p))


def _svc(st, transport, bus=None):
    return WebhookService(endpoints=EndpointStore(st, MemoryCache(), 300), events=EventStore(st),
                          deliveries=DeliveryStore(st), transport=transport,
                          signer=HmacSha256Signer(), config=CFG, event_bus=bus)


def _ep(w, types=("user.created",)):
    return run(w.register(tenant="t1", url="http://127.0.0.1:9/h", event_types=list(types)))


# ── classify ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,ok,expected", [
    (200, True, "delivered"), (204, True, "delivered"),
    (400, True, "terminal"), (401, True, "terminal"), (404, True, "terminal"), (422, True, "terminal"),
    (408, True, "retryable"), (429, True, "retryable"),
    (500, True, "retryable"), (502, True, "retryable"), (503, True, "retryable"),
    (0, False, "retryable"),
])
def test_classify(status, ok, expected):
    assert classify(status, ok) == expected


# ── registration ──────────────────────────────────────────────────────────────
def test_register_rejects_ssrf_url():
    w = _svc(MemoryDataStore(), Transport())
    w._cfg = {**CFG, "allow_private_ips": False}
    with pytest.raises(SsrfBlocked):
        run(w.register(tenant="t1", url="http://169.254.169.254/", event_types=["*"]))


def test_public_view_never_exposes_secrets():
    w = _svc(MemoryDataStore(), Transport())
    ep = _ep(w)
    assert "secrets" not in ep.public() and ep.public()["has_secret"] is True


def test_secret_rotation_keeps_previous_with_a_timestamp_expiry():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    ep = _ep(w)
    old = ep.secrets[0]
    updated, fresh = run(w.rotate_secret(ep.id))
    # The previous secret is kept AND stamped with an expiry — an overlap that ends on its own.
    assert updated.secrets == [fresh, old] and fresh != old
    assert updated.prev_secret_expires > 0


def test_secret_rotation_with_zero_overlap_is_an_immediate_cutover():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    ep = _ep(w)
    updated, fresh = run(w.rotate_secret(ep.id, overlap_seconds=0))
    # A leaked secret: no previous survives, nothing to expire.
    assert updated.secrets == [fresh] and updated.prev_secret_expires == 0.0


def test_a_second_rotation_inside_a_window_drops_the_oldest():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    ep = _ep(w)
    _, s2 = run(w.rotate_secret(ep.id))
    updated, s3 = run(w.rotate_secret(ep.id))
    # Only one previous is ever live — the original is gone, s2 is the single kept previous.
    assert updated.secrets == [s3, s2]


def test_delivery_dual_signs_within_the_overlap_then_stops_after_it_expires():
    st = MemoryDataStore()
    tr = Transport()
    w = _svc(st, tr)
    ep = _ep(w)
    old = ep.secrets[0]
    _, fresh = run(w.rotate_secret(ep.id, overlap_seconds=3600))

    # Inside the window: a receiver still on the OLD secret verifies, and so does one on the new.
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={"a": 1}))
    run(w.deliver(r["deliveries"][0]))
    sent = tr.sent[-1]
    ts = int(sent["headers"]["X-Webhook-Timestamp"])
    sig = sent["headers"]["X-Webhook-Signature"]
    assert signing.verify(sent["body"], sig, [old], now=ts)      # old still accepted
    assert signing.verify(sent["body"], sig, [fresh], now=ts)    # new accepted

    # Past the window: the previous secret lapses by timestamp — deliveries no longer carry it.
    updated = run(w._endpoints.get(ep.id))
    updated.prev_secret_expires = ts - 10          # force the window closed
    run(w._endpoints.save(updated))
    r2 = run(w.dispatch(tenant="t1", event_type="user.created", payload={"a": 2}))
    run(w.deliver(r2["deliveries"][0]))
    sent2 = tr.sent[-1]
    ts2 = int(sent2["headers"]["X-Webhook-Timestamp"])
    sig2 = sent2["headers"]["X-Webhook-Signature"]
    assert not signing.verify(sent2["body"], sig2, [old], now=ts2)   # old no longer signed with
    assert signing.verify(sent2["body"], sig2, [fresh], now=ts2)


# ── dispatch ──────────────────────────────────────────────────────────────────
def test_dispatch_matches_only_subscribed_types():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    _ep(w, types=["user.created"])
    assert run(w.dispatch(tenant="t1", event_type="user.created", payload={}))["subscribers"] == 1
    assert run(w.dispatch(tenant="t1", event_type="user.deleted", payload={}))["subscribers"] == 0


def test_wildcard_subscription():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    _ep(w, types=["*"])
    assert run(w.dispatch(tenant="t1", event_type="anything", payload={}))["subscribers"] == 1


def test_dispatch_is_tenant_scoped():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    _ep(w)
    assert run(w.dispatch(tenant="other", event_type="user.created", payload={}))["subscribers"] == 0


def test_oversized_payload_rejected():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    _ep(w)
    with pytest.raises(ValueError, match="PAYLOAD_TOO_LARGE"):
        run(w.dispatch(tenant="t1", event_type="user.created", payload={"x": "y" * 2000}))


# ── delivery ──────────────────────────────────────────────────────────────────
def test_delivery_signs_the_exact_bytes_sent_and_pins_event_id():
    st = MemoryDataStore()
    tr = Transport()
    w = _svc(st, tr)
    ep = _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={"a": 1}))
    assert run(w.deliver(r["deliveries"][0]))["status"] == DELIVERED
    sent = tr.sent[-1]
    ts = int(sent["headers"]["X-Webhook-Timestamp"])
    assert signing.verify(sent["body"], sent["headers"]["X-Webhook-Signature"], ep.secrets, now=ts)
    assert sent["headers"]["Idempotency-Key"] == r["event_id"]
    assert sent["headers"]["X-Webhook-Id"] == r["event_id"]


def test_event_id_is_stable_across_retries():
    st = MemoryDataStore()
    tr = Transport(status=500)
    w = _svc(st, tr)
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    d = r["deliveries"][0]
    run(w.deliver(d))
    run(w.deliver(d))
    ids = {s["headers"]["Idempotency-Key"] for s in tr.sent}
    assert ids == {r["event_id"]}          # NEVER regenerated per attempt


def test_terminal_4xx_goes_straight_to_dead_letter():
    st = MemoryDataStore()
    w = _svc(st, Transport(status=400))
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    out = run(w.deliver(r["deliveries"][0]))
    assert out["status"] == DEAD and out["attempt"] == 1        # no pointless retries


def test_retryable_failure_schedules_a_jittered_retry():
    st = MemoryDataStore()
    w = _svc(st, Transport(status=503))
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    out = run(w.deliver(r["deliveries"][0]))
    # attempt 1 → full jitter over [0, base·2¹] = [0, 60]
    assert out["status"] == "failed" and 0 < out["retry_in"] <= 60


def test_retries_exhaust_into_dead_letter_never_dropped():
    st, bus = MemoryDataStore(), Bus()
    w = _svc(st, Transport(status=503), bus)
    ep = _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    d = r["deliveries"][0]
    for _ in range(CFG["retry_max_attempts"]):
        e = run(w._endpoints.get(ep.id))          # keep the circuit closed for this test
        e.circuit_state, e.consecutive_failures = "closed", 0
        run(w._endpoints.save_state(e))
        out = run(w.deliver(d))
    assert out["status"] == DEAD and out["error"] == "retries_exhausted"
    # (rows, cursor): the DLQ is paged now — the cursor used to be discarded, on the one
    # queue whose whole purpose is working through a backlog until it is empty.
    dead, _ = run(w._deliveries.dead_letters("t1"))
    assert len(dead) == 1
    assert any(e == "webhook.dead_lettered" for e, _ in bus.events)


def test_circuit_open_keeps_delivery_queued_not_dropped():
    st = MemoryDataStore()
    w = _svc(st, Transport(status=503))
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    d = r["deliveries"][0]
    run(w.deliver(d))
    run(w.deliver(d))                               # threshold=2 → circuit opens
    out = run(w.deliver(d))
    assert out["reason"] == "circuit_open" and out["status"] == "queued"


def test_ssrf_at_delivery_time_is_terminal():
    """DNS can change between retries — re-validation must kill the delivery, not retry it."""
    st = MemoryDataStore()
    w = _svc(st, Transport())
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    w._cfg = {**CFG, "allow_private_ips": False}     # the endpoint now resolves somewhere private
    out = run(w.deliver(r["deliveries"][0]))
    assert out["status"] == DEAD and "ssrf_blocked" in out["error"]


def test_disabled_endpoint_is_not_delivered():
    st = MemoryDataStore()
    w = _svc(st, Transport())
    ep = _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    run(w.update(ep.id, enabled=False))
    assert run(w.deliver(r["deliveries"][0]))["reason"] == "endpoint_disabled"


def test_delivered_is_idempotent():
    st = MemoryDataStore()
    tr = Transport()
    w = _svc(st, tr)
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    d = r["deliveries"][0]
    run(w.deliver(d))
    run(w.deliver(d))                                # second call must not re-send
    assert len(tr.sent) == 1


# ── replay ────────────────────────────────────────────────────────────────────
def test_replay_a_dead_letter_after_recovery():
    st = MemoryDataStore()
    tr = Transport(status=400)
    w = _svc(st, tr)
    _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    d = r["deliveries"][0]
    assert run(w.deliver(d))["status"] == DEAD
    tr.status = 200
    assert run(w.replay(d))["status"] == DELIVERED
    assert run(w._deliveries.dead_letters("t1"))[0] == []


def test_replay_event_replays_every_delivery():
    st = MemoryDataStore()
    tr = Transport(status=400)
    w = _svc(st, tr)
    _ep(w)
    run(w.register(tenant="t1", url="http://127.0.0.1:10/h", event_types=["user.created"]))
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    for d in r["deliveries"]:
        run(w.deliver(d))
    tr.status = 200
    out = run(w.replay_event(r["event_id"], is_operator=True))   # operator replays every delivery (webhook-008: default is now fail-closed)
    assert out["replayed"] == 2 and out["delivered"] == 2


def test_replay_unknown_delivery():
    w = _svc(MemoryDataStore(), Transport())
    assert run(w.replay("nope"))["ok"] is False


# ── health ────────────────────────────────────────────────────────────────────
def test_endpoint_health_reports_success_rate_and_circuit():
    st = MemoryDataStore()
    tr = Transport()
    w = _svc(st, tr)
    ep = _ep(w)
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={}))
    run(w.deliver(r["deliveries"][0]))
    h = run(w.health(ep.id))
    assert h["success_count"] == 1 and h["success_rate"] == 1.0 and h["circuit_state"] == "closed"
    assert run(w.health("nope")) is None


# ── cross-tenant IDOR on replay_event ────────────────────────────────────────
def test_replay_event_is_tenant_scoped():
    st = MemoryDataStore()
    tr = Transport()
    w = _svc(st, tr)
    _ep(w)                                                    # t1 endpoint on user.created
    r = run(w.dispatch(tenant="t1", event_type="user.created", payload={"a": 1}))
    event_id = r.get("event_id") or r.get("event", {}).get("id")
    assert event_id, f"need an event id from dispatch: {r}"
    before = len(tr.sent)
    # a tenant-scoped caller from t2 must replay NONE of t1's deliveries
    out = run(w.replay_event(event_id, tenant="t2", is_operator=False))
    assert out["replayed"] == 0 and len(tr.sent) == before   # nothing re-fired
    # the owning tenant (or a platform operator) can
    out2 = run(w.replay_event(event_id, tenant="t1", is_operator=False))
    assert out2["replayed"] >= 1
