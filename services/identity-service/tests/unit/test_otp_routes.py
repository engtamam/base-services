"""OTP over HTTP — the round trip a caller actually makes, and the two things a caller must NOT be
able to do: read the code from a response, or tell 'wrong code' apart from 'no such identifier'."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import create_app


class _CapturingNotifier:
    driver = "capture"

    def __init__(self):
        self.last = None

    async def send(self, *, to, template, tenant, variables=None, channel="email"):
        self.last = {"to": to, "template": template, "variables": variables or {},
                     "channel": channel, "tenant": tenant}
        return True


@pytest.fixture()
def client_and_notifier(monkeypatch):
    # Login by phone so the route's canonicalisation folds every spelling to one key.
    monkeypatch.setenv("IDENTITY_LOGIN_FIELD", "phone")
    from app.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()                  # don't leak the phone setting into other tests
    cap = _CapturingNotifier()
    app.state.container.notifier = cap          # intercept delivery to read the code the human'd get
    return TestClient(app), cap


def test_request_then_verify_round_trip(client_and_notifier):
    c, cap = client_and_notifier
    r = c.post("/api/v1/otp/request", json={"identifier": "+966501234567"})
    assert r.status_code == 200 and r.json()["delivered"] is True
    assert cap.last["channel"] == "email"       # default when OTP_DELIVERY_CHANNEL is unset
    assert cap.last["tenant"] == ""             # OTP_NOTIFY_TENANT default — today's behavior
    assert "code" not in r.json()               # the code is NEVER in the response body
    code = cap.last["variables"]["code"]        # only delivery carries it
    ok = c.post("/api/v1/otp/verify", json={"identifier": "0501234567", "code": code})
    assert ok.status_code == 200 and ok.json()["verified"] is True   # any spelling of the phone


def test_otp_delivery_channel_is_the_real_channel_not_just_a_template_var(monkeypatch):
    # OTP_DELIVERY_CHANNEL was READ but only stuffed into a template variable while the channel was
    # hardcoded "email" — so a phone OTP silently went out as email. It must now be the real channel.
    monkeypatch.setenv("IDENTITY_LOGIN_FIELD", "phone")
    monkeypatch.setenv("OTP_DELIVERY_CHANNEL", "whatsapp")
    from app.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()
    cap = _CapturingNotifier()
    app.state.container.notifier = cap
    c = TestClient(app)
    assert c.post("/api/v1/otp/request", json={"identifier": "+966501234567"}).status_code == 200
    assert cap.last["channel"] == "whatsapp"    # the REAL channel reaches the notifier
    assert cap.last["variables"].get("channel") == "whatsapp"   # still exposed for the template too


def test_otp_notify_tenant_is_config_not_a_hardcoded_empty(monkeypatch):
    # The OTP send resolved its template/credentials under tenant="" hardcoded, but a platform
    # console registers them under a reserved tenant — so the send never found them. It is config now.
    monkeypatch.setenv("IDENTITY_LOGIN_FIELD", "phone")
    monkeypatch.setenv("OTP_NOTIFY_TENANT", "_platform")
    from app.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()
    cap = _CapturingNotifier()
    app.state.container.notifier = cap
    c = TestClient(app)
    assert c.post("/api/v1/otp/request", json={"identifier": "+966501234567"}).status_code == 200
    assert cap.last["tenant"] == "_platform"    # the configured tenant reaches the notifier


def test_a_wrong_code_and_an_unknown_identifier_look_identical(client_and_notifier):
    c, cap = client_and_notifier
    c.post("/api/v1/otp/request", json={"identifier": "+966500000000"})
    wrong = c.post("/api/v1/otp/verify", json={"identifier": "+966500000000", "code": "999999"})
    never = c.post("/api/v1/otp/verify", json={"identifier": "+966511111111", "code": "999999"})
    assert wrong.status_code == never.status_code == 400   # no oracle


def test_a_consumed_code_cannot_be_replayed(client_and_notifier):
    c, cap = client_and_notifier
    c.post("/api/v1/otp/request", json={"identifier": "+966522222222"})
    code = cap.last["variables"]["code"]
    assert c.post("/api/v1/otp/verify", json={"identifier": "+966522222222", "code": code}).status_code == 200
    assert c.post("/api/v1/otp/verify", json={"identifier": "+966522222222", "code": code}).status_code == 400
