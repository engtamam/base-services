"""End-to-end (real HTTP routes) for the in-app notification deep_link + kind fields:
POST /internal/notify → GET /api/v1/notifications, plus route-level validation, idempotency,
mark-read preservation, and other-channel ignore. Complements the unit tests in test_deep_link.py."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
                       "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
                       "RATELIMIT_MAX": "1000"})
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    # seed an in-app template once
    admin = {"Authorization": f"Bearer {_tok('admin', caps=['platform.notifications'])}"}
    c.post("/api/v1/templates",
           json={"key": "order_new", "locale": "en", "subject": "New order", "body": "Order $n"},
           headers=admin)
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _tok(sub="u1", caps=None, tenant=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    if tenant is not None:
        claims["tenant"] = tenant
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _user(sub="u1"):
    return {"Authorization": f"Bearer {_tok(sub)}"}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def _notify(client, **over):
    body = {"recipient": "u1", "channel": "inapp", "template_key": "order_new",
            "user_id": "u1", "variables": {"n": "102"}}
    body.update(over)
    return client.post("/internal/notify", json=body, headers=_internal())


def _feed(client, sub="u1"):
    r = client.get("/api/v1/notifications", headers=_user(sub))
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ── happy path: both fields travel notify → feed ──────────────────────────────
def test_action_with_deep_link_round_trips_through_the_routes(client):
    r = _notify(client, user_id="e2e_a", recipient="e2e_a",
                deep_link="/coworking/join-requests/55", kind="action")
    assert r.status_code == 200 and r.json()["status"] == "sent", r.text
    item = _feed(client, "e2e_a")[0]
    assert item["deep_link"] == "/coworking/join-requests/55"
    assert item["kind"] == "action"


def test_info_booking_defaults_when_fields_omitted(client):
    assert _notify(client, user_id="e2e_b", recipient="e2e_b").json()["status"] == "sent"
    item = _feed(client, "e2e_b")[0]
    assert item["kind"] == "info" and item["deep_link"] == ""


def test_info_with_a_deep_link_but_no_decision(client):
    _notify(client, user_id="e2e_c", recipient="e2e_c", deep_link="/coworking/bookings/9", kind="info")
    item = _feed(client, "e2e_c")[0]
    assert item["kind"] == "info" and item["deep_link"] == "/coworking/bookings/9"


# ── route-level validation (422), not just model-level ────────────────────────
@pytest.mark.parametrize("bad", ["ACTION", "decision", "warn"])
def test_route_rejects_invalid_kind(client, bad):
    assert _notify(client, kind=bad).status_code == 422


@pytest.mark.parametrize("bad", ["http://evil.example/x", "//evil", "javascript:alert(1)", "no-slash"])
def test_route_rejects_external_deep_link(client, bad):
    assert _notify(client, deep_link=bad).status_code == 422


# ── idempotency: a deduped retry creates ONE item and keeps its fields ─────────
def test_idempotent_retry_keeps_one_item_with_its_fields(client):
    k = "idem-e2e-1"
    r1 = _notify(client, user_id="e2e_d", recipient="e2e_d", kind="action",
                 deep_link="/coworking/subscriptions/7", idempotency_key=k)
    r2 = _notify(client, user_id="e2e_d", recipient="e2e_d", kind="action",
                 deep_link="/coworking/subscriptions/7", idempotency_key=k)
    assert r1.status_code == 200 and r2.status_code == 200
    items = _feed(client, "e2e_d")
    assert len(items) == 1                       # deduped, not double-created
    assert items[0]["kind"] == "action" and items[0]["deep_link"] == "/coworking/subscriptions/7"


# ── mark-read preserves the new fields ────────────────────────────────────────
def test_mark_read_preserves_kind_and_deep_link(client):
    _notify(client, user_id="e2e_e", recipient="e2e_e", kind="action", deep_link="/coworking/x/1")
    item = _feed(client, "e2e_e")[0]
    assert client.post(f"/api/v1/notifications/{item['id']}/read", headers=_user("e2e_e")).status_code == 200
    after = _feed(client, "e2e_e")[0]
    assert after["read"] is True
    assert after["kind"] == "action" and after["deep_link"] == "/coworking/x/1"


# ── a non-inapp channel accepts the fields but is unaffected ───────────────────
def test_email_channel_accepts_but_ignores_the_fields(client):
    r = _notify(client, channel="email", recipient="a@b.com", user_id=None,
                kind="action", deep_link="/coworking/x/2")
    assert r.status_code == 200 and r.json()["status"] == "sent", r.text
