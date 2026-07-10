"""Security: recipient/channel validation, SSTI inertness, provider request safety."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN)
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


def _internal():
    return {"X-Internal-Token": ITOKEN}


def test_notify_rejects_unknown_channel(client):
    # channel is pattern-validated -> 422
    r = client.post("/internal/notify",
                    json={"recipient": "a@b.com", "channel": "carrier-pigeon", "template_key": "x"},
                    headers=_internal())
    assert r.status_code == 422


def test_notify_rejects_empty_recipient(client):
    r = client.post("/internal/notify",
                    json={"recipient": "", "channel": "email", "template_key": "x"},
                    headers=_internal())
    assert r.status_code == 422


def test_ssti_value_is_not_evaluated(client):
    # An operator template + an attacker-supplied variable that looks like an
    # expression must render literally (no code execution).
    from app.adapters.renderer.safe import SafeRenderer
    _, body = SafeRenderer().render("s", "$v", {"v": "${7*7}"})
    assert body == "${7*7}" and body != "49"


def test_msegat_ksa_request_shape(monkeypatch):
    import app.adapters.sms.msegat as m
    captured = {}

    def fake_post_json(url, payload, headers=None):
        captured.update(url=url, payload=payload)
        return 200, "{}"

    monkeypatch.setattr(m, "post_json", fake_post_json)
    m.MsegatSms("user", "key", "Brand").send("966500000000", "hi")
    assert captured["url"] == "https://www.msegat.com/gw/sendsms.php"
    assert captured["payload"]["userName"] == "user"
    assert captured["payload"]["userSender"] == "Brand"


def test_twilio_uses_basic_auth(monkeypatch):
    import app.adapters.sms.twilio as t
    captured = {}

    def fake_post_form(url, fields, headers=None):
        captured.update(url=url, fields=fields, headers=headers)
        return 201, "{}"

    monkeypatch.setattr(t, "post_form", fake_post_form)
    t.TwilioSms("ACsid", "token", "+15550000000").send("+966500000000", "hi")
    assert "/Accounts/ACsid/Messages.json" in captured["url"]
    assert captured["headers"]["Authorization"].startswith("Basic ")
    assert captured["fields"]["To"] == "+966500000000"
