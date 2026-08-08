"""Integration: internal notify + user feed/preferences + template admin."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
        "RATELIMIT_MAX": "1000",
    }
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _token(sub="u1", caps=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _user(sub="u1", caps=None):
    return {"Authorization": f"Bearer {_token(sub, caps)}"}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def _seed_template(client, key="welcome", locale="en", subject="Hi $name", body="Hello $name"):
    admin = _user("admin", caps=["notifications.templates"])
    return client.post("/api/v1/templates",
                       json={"key": key, "locale": locale, "subject": subject, "body": body},
                       headers=admin)


def test_template_admin_requires_capability(client):
    # no caps -> 403
    assert client.post("/api/v1/templates",
                       json={"key": "k", "locale": "en", "subject": "s", "body": "b"},
                       headers=_user("u", caps=[])).status_code == 403
    # with cap -> 200
    assert _seed_template(client).status_code == 200


def test_internal_notify_requires_token(client):
    assert client.post("/internal/notify",
                       json={"recipient": "a@b.com", "channel": "email", "template_key": "welcome"}
                       ).status_code == 401


def test_internal_notify_sends_and_records(client):
    _seed_template(client)
    r = client.post("/internal/notify",
                    json={"recipient": "a@b.com", "channel": "email",
                          "template_key": "welcome", "variables": {"name": "Sara"}},
                    headers=_internal())
    assert r.status_code == 200
    assert r.json()["status"] == "sent"
    assert r.json()["provider"] == "console"


def test_consent_gated_send_reaches_the_gate_with_the_body_tenant():
    """The blocking bug: NotifyIn declared no `tenant`, so pydantic silently DROPPED the caller's
    tenant. Consent is keyed by (tenant, subject, purpose), so every gated send asked about an empty
    tenant, found no record, and was denied forever — a permanent silent deny for all marketing.
    Prove the body tenant now reaches the gate and a GRANTED purpose sends."""
    from app.ports.consent_gate import ALLOWED, ConsentVerdict

    saved = dict(os.environ)
    try:
        c = _make_app(NOTIFY_CONSENT_PURPOSES='{"winback": "marketing"}')
        _seed_template(c, key="winback")

        class _Gate:
            driver = "fake"

            def __init__(self):
                self.asked: list[tuple[str, str, str]] = []

            async def check(self, *, tenant, subject, purpose):
                self.asked.append((tenant, subject, purpose))
                return ConsentVerdict(status=ALLOWED, reason="granted")

        gate = _Gate()
        c.app.state.container.consent_gate = gate

        r = c.post("/internal/notify",
                   json={"recipient": "a@b.com", "channel": "email", "template_key": "winback",
                         "tenant": "alpha", "user_id": "u_9", "variables": {"name": "S"}},
                   headers=_internal())
        assert r.status_code == 200
        assert r.json()["status"] == "sent"                    # granted -> SENT, not consent_denied
        assert gate.asked == [("alpha", "u_9", "marketing")]   # the BODY tenant reached the gate

        # A gated send with NO tenant is a misconfiguration, named distinctly — never a plausible
        # consent_denied, and the gate is not even asked with an empty tenant.
        gate.asked.clear()
        r2 = c.post("/internal/notify",
                    json={"recipient": "a@b.com", "channel": "email", "template_key": "winback",
                          "user_id": "u_9", "variables": {"name": "S"}},
                    headers=_internal())
        assert r2.json()["status"] == "consent_no_tenant"
        assert gate.asked == []
    finally:
        os.environ.clear()
        os.environ.update(saved)
        from app.config import get_settings
        get_settings.cache_clear()


def test_operator_delivery_log_masks_recipients_and_needs_the_cap(client):
    _seed_template(client)
    client.post("/internal/notify",
                json={"recipient": "sara@example.com", "channel": "email",
                      "template_key": "welcome", "variables": {"name": "Sara"}},
                headers=_internal())

    op = _user("op", caps=["notifications.templates"])
    r = client.get("/api/v1/notifications/deliveries", headers=op)
    assert r.status_code == 200
    rows = r.json()["deliveries"]
    assert rows, "the delivery was recorded but the log is empty"
    # The recipient is masked — enough to correlate, never a harvestable address.
    assert all("sara@example.com" not in d["recipient"] for d in rows)
    assert any(d["recipient"] == "s***@example.com" for d in rows)

    # A recipient (self) capability must not reach the operator delivery log.
    assert client.get("/api/v1/notifications/deliveries",
                      headers=_user("u", caps=["notifications.self"])).status_code == 403


def test_inapp_feed_and_read(client):
    _seed_template(client, key="ping", subject="Title", body="Body $x")
    client.post("/internal/notify",
                json={"recipient": "u1", "channel": "inapp", "template_key": "ping",
                      "variables": {"x": "1"}, "user_id": "u1"},
                headers=_internal())
    feed = client.get("/api/v1/notifications", headers=_user("u1"))
    assert feed.status_code == 200
    items = feed.json()["items"]
    assert len(items) >= 1
    item_id = items[0]["id"]
    assert client.post(f"/api/v1/notifications/{item_id}/read", headers=_user("u1")).status_code == 200


def test_preferences_get_put(client):
    got = client.get("/api/v1/notifications/preferences", headers=_user("u2"))
    assert got.status_code == 200 and got.json()["suppressed"] is False
    put = client.put("/api/v1/notifications/preferences",
                     json={"suppressed": True, "opt_out_channels": ["sms"], "opt_out_events": []},
                     headers=_user("u2"))
    assert put.status_code == 200 and put.json()["suppressed"] is True


def test_feed_requires_auth(client):
    assert client.get("/api/v1/notifications").status_code == 401
