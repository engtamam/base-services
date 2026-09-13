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
    # Enable the free-text connection-test diagnostic so its send-path tests exercise a real send;
    # the default-OFF security posture is covered separately (test_wa_test_send_free_text_off_by_default).
    c = _make_app(NOTIFY_TEST_SEND_ALLOW_FREE_TEXT="true")
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _token(sub="u1", caps=None, tenant=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    if tenant is not None:
        claims["tenant"] = tenant
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _user(sub="u1", caps=None, tenant=None):
    return {"Authorization": f"Bearer {_token(sub, caps, tenant)}"}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def _seed_template(client, key="welcome", locale="en", subject="Hi $name", body="Hello $name"):
    # The GLOBAL template catalogue is a platform surface now (a shop cannot overwrite shared templates).
    admin = _user("admin", caps=["platform.notifications"])
    return client.post("/api/v1/templates",
                       json={"key": key, "locale": locale, "subject": subject, "body": body},
                       headers=admin)


def test_template_admin_requires_capability(client):
    # no caps -> 403
    assert client.post("/api/v1/templates",
                       json={"key": "k", "locale": "en", "subject": "s", "body": "b"},
                       headers=_user("u", caps=[])).status_code == 403
    # the per-tenant WhatsApp-template cap is NOT enough for the shared global catalogue (escalation
    # fix): a shop must not overwrite templates every tenant renders from.
    assert client.post("/api/v1/templates",
                       json={"key": "k", "locale": "en", "subject": "s", "body": "b"},
                       headers=_user("u", caps=["notifications.templates"])).status_code == 403
    # the platform capability -> 200
    assert _seed_template(client).status_code == 200


def test_wa_test_send_free_text_off_by_default():
    # Free-text diagnostic sends are a spam vector and OFF unless an operator opts in.
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        # Explicitly false (the module `client` fixture leaves it "true" in os.environ otherwise).
        app = _make_app(NOTIFY_TEST_SEND_ALLOW_FREE_TEXT="false")
        app.app.state.provider_pool = _pool_with(_FakeWaDriver("wamid.X"))
        r = app.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid",
                                "mode": "text", "body": "hi"},
                     headers=_user("op", caps=["notifications.templates"], tenant="acme"))
        assert r.status_code == 403      # free-text refused by default (detail masked to FORBIDDEN)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


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


def test_internal_whatsapp_template_list_is_token_authed_and_tenant_scoped(client):
    # A peer service that picks a template by TYPE needs to prefer an APPROVED one before it spends
    # a send and a daily-cap slot. This is the internal counterpart to the user-tier list: a service
    # token, tenant as a parameter — no user JWT (whose signing key stays in identity-service).
    admin = _user("op", caps=["notifications.templates"], tenant="acme")
    reg = client.post("/api/v1/whatsapp/templates", headers=admin,
                      json={"name": "order_ready", "language": "en", "category": "UTILITY",
                            "body": "Order {{1}} ready", "variables": ["order"],
                            "examples": ["A-100"]})
    assert reg.status_code == 201, reg.text

    # No token -> refused; no tenant -> 400 (an empty scope is every customer).
    assert client.get("/internal/whatsapp/templates?tenant=acme").status_code == 401
    assert client.get("/internal/whatsapp/templates", headers=_internal()).status_code == 400

    # Token + tenant -> the tenant's templates, and only theirs.
    got = client.get("/internal/whatsapp/templates?tenant=acme", headers=_internal())
    assert got.status_code == 200
    names = [t["name"] for t in got.json()["templates"]]
    assert "order_ready" in names
    other = client.get("/internal/whatsapp/templates?tenant=globex", headers=_internal())
    assert other.json()["templates"] == []      # another tenant sees nothing of acme's


def test_every_engine_channel_passes_validation(client):
    """The one send path must accept EXACTLY the channels the engine supports — the failure class
    here is a validator that restated core/notifier.CHANNELS and fell behind it, silently rejecting
    `whatsapp` (the channel this platform is built around) at the door while the engine supported it.
    """
    from app.core.notifier import CHANNELS
    _seed_template(client)
    for ch in CHANNELS:
        r = client.post("/internal/notify",
                        json={"recipient": "a@b.com", "channel": ch, "template_key": "welcome"},
                        headers=_internal())
        # Never a 422: every declared channel reaches the notifier. It may then report `failed` when
        # no provider is configured in this test env — that is a delivery outcome, not a rejection.
        assert r.status_code == 200, f"channel {ch!r} was rejected before the engine: {r.status_code} {r.text}"


def test_an_unsupported_channel_is_still_rejected(client):
    """The pattern is derived, not open: a channel the engine does NOT know is still a 422."""
    r = client.post("/internal/notify",
                    json={"recipient": "a@b.com", "channel": "carrier-pigeon",
                          "template_key": "welcome"},
                    headers=_internal())
    assert r.status_code == 422


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

    # The cross-tenant delivery log is a platform surface (masked recipients across every tenant).
    op = _user("op", caps=["platform.notifications"])
    r = client.get("/api/v1/notifications/deliveries", headers=op)
    assert r.status_code == 200
    # the per-tenant WhatsApp-template cap must NOT reach the cross-tenant delivery log
    assert client.get("/api/v1/notifications/deliveries",
                      headers=_user("op2", caps=["notifications.templates"])).status_code == 403
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


def test_internal_whatsapp_messaging_limit_is_token_authed_and_tenant_scoped(client):
    # A sending service needs the ceiling BEFORE the send — to reserve the day's capacity and size a
    # campaign — not from Meta's rejection after it is spent. Internal, token-authed, per tenant.
    assert client.get("/internal/whatsapp/messaging-limit?tenant=acme").status_code == 401  # no token
    assert client.get("/internal/whatsapp/messaging-limit",
                      headers=_internal()).status_code == 400                                # no tenant
    r = client.get("/internal/whatsapp/messaging-limit?tenant=acme", headers=_internal())
    assert r.status_code == 200
    body = r.json()
    # test default driver is console → reports unlimited/green (dev has no Meta portfolio)
    assert body["ok"] and body["tier"] == "TIER_UNLIMITED" and body["limit"] == -1
    assert body["quality_rating"] == "GREEN"
# ── WhatsApp admin diagnostic test-send (bypasses the notify chain) ────────────
# whatsapp_for_identity returns a FailoverWhatsApp (the (ok, driver, message_id) TUPLE contract),
# NOT a bare driver — so these tests wrap the fake DRIVER in a REAL FailoverWhatsApp, exactly as the
# composition root does. A fake that returned a bare string is what let the v0.18.17 bug (route
# treating the tuple as a scalar, 200-ing a Meta reject) ship unnoticed.
from app.adapters.failover import FailoverWhatsApp


class _FakeWaDriver:
    """A single WhatsApp DRIVER (like MetaWhatsApp): returns a scalar wamid; "" == send failure."""
    driver = "meta"

    def __init__(self, mid="wamid.OK"):
        self._mid = mid
        self.calls = []

    def send_text(self, to, body, biz_opaque_callback_data=""):
        self.calls.append(("text", to, body))
        return self._mid

    def send(self, to, template_name, language, variables, biz_opaque_callback_data="",
             flow_token="", flow_action_data=None, header_media_id=""):
        self.calls.append(("template", to, template_name))
        return self._mid


class _TestSendPool:
    def __init__(self, provider):
        self._p = provider   # a FailoverWhatsApp (what whatsapp_for_identity really returns) or None

    async def whatsapp_for_identity(self, tenant, phone_number_id):
        return self._p


_TS = "/api/v1/whatsapp/templates/test-send"


def _pool_with(driver):
    return _TestSendPool(FailoverWhatsApp([driver]))


def test_wa_test_send_text_ok(client):
    drv = _FakeWaDriver("wamid.OK")
    client.app.state.provider_pool = _pool_with(drv)
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid",
                               "mode": "text", "body": "hi there"},
                    headers=_user("op", caps=["notifications.templates"], tenant="acme"))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["message_id"] == "wamid.OK"   # the real wamid, unpacked
    assert drv.calls[-1] == ("text", "+966500001234", "hi there")


def test_wa_test_send_template_mode_ok(client):
    drv = _FakeWaDriver("wamid.T")
    client.app.state.provider_pool = _pool_with(drv)
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid", "mode": "template",
                               "template_name": "auth_otp", "language": "en", "variables": ["123456"]},
                    headers=_user("op", caps=["notifications.templates"], tenant="acme"))
    assert r.status_code == 200 and r.json()["message_id"] == "wamid.T"
    assert drv.calls[-1] == ("template", "+966500001234", "auth_otp")


def test_wa_test_send_not_connected_is_409(client):
    client.app.state.provider_pool = _TestSendPool(None)
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "not-mine",
                               "mode": "text", "body": "hi"},
                    headers=_user("op", caps=["notifications.templates"], tenant="acme"))
    assert r.status_code == 409 and r.json()["reason"] == "NOT_CONNECTED"


def test_wa_test_send_reject_is_502_not_a_false_success(client):
    # THE v0.18.17 BUG: a real Meta reject (e.g. an unapproved template) comes back from the failover
    # as (False, "meta", "") — a NON-EMPTY tuple. The route must read `ok`, not the tuple's truthiness,
    # or it reports 200 with a non-wamid id for a message that never went out.
    client.app.state.provider_pool = _pool_with(_FakeWaDriver(""))
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid",
                               "mode": "text", "body": "hi"},
                    headers=_user("op", caps=["notifications.templates"], tenant="acme"))
    assert r.status_code == 502 and r.json()["reason"] == "SEND_FAILED"


def test_wa_test_send_missing_body_is_422(client):
    client.app.state.provider_pool = _pool_with(_FakeWaDriver())
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid", "mode": "text"},
                    headers=_user("op", caps=["notifications.templates"], tenant="acme"))
    assert r.status_code == 422


def test_wa_test_send_requires_the_capability(client):
    client.app.state.provider_pool = _pool_with(_FakeWaDriver())
    r = client.post(_TS, json={"to": "+966500001234", "phone_number_id": "pid",
                               "mode": "text", "body": "hi"},
                    headers=_user("u", caps=[], tenant="acme"))
    assert r.status_code == 403
