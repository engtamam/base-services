"""Ingest idempotency — the property that lets the sink retry a transient timeout safely.

A retried append carries the same idempotency_key: the log returns the ORIGINAL receipt and appends
NOTHING to the tamper-evident chain. A different key appends. No key is the legacy one-shot. This is
what makes a slow audit call no longer fail the real user action it was witnessing.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN, "PII_SALT": "s" * 32,
           "AUDIT_SINK_DRIVER": "null", "RATELIMIT_MAX": "1000"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


_BODY = {"service": "identity", "action": "auth.login_succeeded", "actor": "u1", "tenant": "acme"}


def test_a_retry_with_the_same_key_returns_the_original_receipt_and_appends_nothing(client):
    a = client.post("/internal/audit", headers=_h(), json={**_BODY, "idempotency_key": "k1"}).json()
    b = client.post("/internal/audit", headers=_h(), json={**_BODY, "idempotency_key": "k1"}).json()
    assert a["seq"] == b["seq"] and a["hash"] == b["hash"] and a["id"] == b["id"]
    assert a["duplicate"] is False and b["duplicate"] is True
    # The chain did NOT advance — a second, different-key event lands at seq a+1, not a+2.
    c = client.post("/internal/audit", headers=_h(), json={**_BODY, "idempotency_key": "k2"}).json()
    assert c["seq"] == a["seq"] + 1 and c["duplicate"] is False


def test_same_client_key_across_services_or_tenants_does_not_drop_an_event(client):
    """Two callers reusing the same idempotency_key both append — the key is namespaced.

    Before namespacing, caller B's append matched caller A's reservation, returned A's receipt with
    duplicate=true, and added NOTHING to the tamper-evident chain: a silently dropped audit event.
    """
    a = client.post("/internal/audit", headers=_h(),
                    json={**_BODY, "idempotency_key": "shared"}).json()
    # Same key, DIFFERENT service — must be a distinct event, not a duplicate of A.
    b = client.post("/internal/audit", headers=_h(),
                    json={**_BODY, "service": "billing", "idempotency_key": "shared"}).json()
    # Same key, DIFFERENT tenant — likewise distinct.
    d = client.post("/internal/audit", headers=_h(),
                    json={**_BODY, "tenant": "globex", "idempotency_key": "shared"}).json()
    assert b["duplicate"] is False and d["duplicate"] is False
    assert len({a["id"], b["id"], d["id"]}) == 3          # three real, distinct events
    # ...and A's OWN retry (same service+tenant+key) is still deduped.
    a2 = client.post("/internal/audit", headers=_h(),
                     json={**_BODY, "idempotency_key": "shared"}).json()
    assert a2["duplicate"] is True and a2["id"] == a["id"]


def test_no_key_is_the_legacy_one_shot(client):
    r = client.post("/internal/audit", headers=_h(), json=_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body.get("seq") and body["duplicate"] is False


def test_a_concurrent_reservation_is_asked_to_retry_not_duplicated(client):
    # Directly exercise the race guard: reserve the key as an in-flight (pending) attempt, then a
    # second attempt with that key must be told to retry (503) rather than append a duplicate.
    import asyncio
    from app.api.v1.audit import build_audit

    # The stored key is namespaced by (service, tenant), so reserve THAT, not the raw client key.
    ns = f"{len('identity')}:identity|{len('acme')}:acme|krace"

    async def reserve():
        # Reach the repo the way a request would, and stamp a FRESH pending reservation.
        req = type("R", (), {"app": client.app})()
        audit = build_audit(req)
        import time as _t
        await audit._repo.idempo_reserve(ns, _t.time())
    asyncio.new_event_loop().run_until_complete(reserve())
    r = client.post("/internal/audit", headers=_h(), json={**_BODY, "idempotency_key": "krace"})
    # 503, not a duplicate append: the sink retries on 503 (with the same key), and a later attempt
    # returns the committed receipt. Exactly one event ever lands, even under the slow-append race.
    assert r.status_code == 503
