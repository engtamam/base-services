"""Integration: endpoints CRUD, dispatch→deliver, DLQ/replay, health, inbound ingest, gating."""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core import signing
from app.ports.webhook_transport import Response

SECRET = "h" * 40
ITOKEN = "i" * 40
INBOUND_SECRET = "whsec_test"


class Transport:
    driver = "mock"

    def __init__(self):
        self.status, self.ok, self.sent = 200, True, []

    def send(self, **k):
        self.sent.append(k)
        return Response(ok=self.ok, status=self.status, body="ok", latency_ms=2)


def _make_app():
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000",
                      WEBHOOK_REQUIRE_HTTPS="false", WEBHOOK_ALLOW_PRIVATE_IPS="true",
                      INBOUND_SECRETS=f"stripe={INBOUND_SECRET}", CIRCUIT_FAILURE_THRESHOLD="99")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.state.container.webhook_transport = Transport()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _adm(caps=("platform.webhooks",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "admin", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _int():
    return {"X-Internal-Token": ITOKEN}


def _register(client, types=("user.created",), url="http://127.0.0.1:9/hook"):
    return client.post("/api/v1/webhooks/endpoints",
                       json={"url": url, "event_types": list(types)}, headers=_adm()).json()


def test_register_returns_secret_once_then_hides_it(client):
    ep = _register(client)
    assert ep["secret"].startswith("whsec_") and "secrets" not in ep
    listed = client.get("/api/v1/webhooks/endpoints", headers=_adm()).json()["endpoints"]
    assert all("secret" not in e and "secrets" not in e for e in listed)


def test_register_rejects_ssrf_url(client):
    r = client.post("/api/v1/webhooks/endpoints",
                    json={"url": "http://169.254.169.254/", "event_types": ["*"]}, headers=_adm())
    # 400 (not 201, not a 5xx) — the template deliberately doesn't echo the detail string
    assert r.status_code == 400


def test_dispatch_and_deliver(client):
    ep = _register(client, url="http://127.0.0.1:9101/hook")
    r = client.post("/internal/webhooks/dispatch",
                    json={"tenant": "t1", "type": "user.created", "payload": {"a": 1}}, headers=_int())
    assert r.status_code == 202
    body = r.json()
    assert body["subscribers"] >= 1
    out = client.post(f"/internal/webhooks/deliver/{body['deliveries'][0]}", headers=_int()).json()
    assert out["status"] == "delivered"


def test_endpoint_health(client):
    ep = _register(client, url="http://127.0.0.1:9102/hook")
    r = client.post("/internal/webhooks/dispatch",
                    json={"tenant": "t1", "type": "user.created", "payload": {}}, headers=_int()).json()
    for d in r["deliveries"]:
        client.post(f"/internal/webhooks/deliver/{d}", headers=_int())
    h = client.get(f"/api/v1/webhooks/endpoints/{ep['id']}/health", headers=_adm()).json()
    assert h["circuit_state"] == "closed" and h["success_count"] >= 1


def test_rotate_secret_keeps_previous(client):
    ep = _register(client, url="http://127.0.0.1:9103/hook")
    out = client.post(f"/api/v1/webhooks/endpoints/{ep['id']}/rotate-secret", headers=_adm()).json()
    assert out["secret"] != ep["secret"] and out["previous_still_accepted"] is True


def test_dead_letter_and_replay(client):
    app = client.app
    transport = app.state.container.webhook_transport
    ep = _register(client, url="http://127.0.0.1:9104/hook")
    transport.status = 400                              # terminal → straight to DLQ
    r = client.post("/internal/webhooks/dispatch",
                    json={"tenant": "t1", "type": "user.created", "payload": {}}, headers=_int()).json()
    d = r["deliveries"][0]
    assert client.post(f"/internal/webhooks/deliver/{d}", headers=_int()).json()["status"] == "dead"
    dlq = client.get("/api/v1/webhooks/dead-letters", headers=_adm()).json()["dead_letters"]
    assert any(x["id"] == d for x in dlq)
    transport.status = 200                              # endpoint recovers
    assert client.post(f"/api/v1/webhooks/deliveries/{d}/replay", headers=_adm()).json()["status"] == "delivered"


def test_oversized_dispatch_rejected(client):
    r = client.post("/internal/webhooks/dispatch",
                    json={"tenant": "t1", "type": "x", "payload": {"b": "y" * 2_000_000}}, headers=_int())
    assert r.status_code == 413


# ── inbound ───────────────────────────────────────────────────────────────────
def _inbound(client, payload, secret=INBOUND_SECRET, ts=None, source="stripe"):
    body = json.dumps(payload).encode()
    ts = int(time.time()) if ts is None else ts
    return client.post(f"/inbound/{source}", content=body,
                       headers={"Content-Type": "application/json",
                                "X-Webhook-Signature": signing.sign(body, secret, ts)})


def test_inbound_accepts_valid_signature_and_dedupes(client):
    r = _inbound(client, {"id": "evt_100", "type": "ping"})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    again = _inbound(client, {"id": "evt_100", "type": "ping"})
    assert again.json()["duplicate"] is True


def test_inbound_rejects_bad_signature(client):
    assert _inbound(client, {"id": "evt_101"}, secret="wrong").status_code == 400


def test_inbound_rejects_replayed_timestamp(client):
    assert _inbound(client, {"id": "evt_102"}, ts=int(time.time()) - 3600).status_code == 400


def test_inbound_unknown_source_rejected(client):
    assert _inbound(client, {"id": "evt_103"}, source="mystery").status_code == 400


def test_inbound_invalid_json_rejected(client):
    r = client.post("/inbound/stripe", content=b"not json",
                    headers={"X-Webhook-Signature": "t=1,v1=x"})
    assert r.status_code == 400


# ── authz ─────────────────────────────────────────────────────────────────────
def test_read_cap_cannot_write(client):
    ro = _adm(caps=("webhooks.read",))
    assert client.get("/api/v1/webhooks/endpoints", headers=ro).status_code == 200
    assert client.post("/api/v1/webhooks/endpoints",
                       json={"url": "http://127.0.0.1:9/h"}, headers=ro).status_code == 403


def test_cross_tenant_endpoint_hidden(client):
    ep = _register(client, url="http://127.0.0.1:9105/hook")
    other = _adm(caps=("webhooks.manage",), tenant="intruder")
    assert client.get(f"/api/v1/webhooks/endpoints/{ep['id']}/health", headers=other).status_code == 404
    assert client.delete(f"/api/v1/webhooks/endpoints/{ep['id']}", headers=other).status_code == 404
