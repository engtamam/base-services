"""Phone-number registration — the PIN-carrying step, and the THREE distinct failures.

A tenant that finished Embedded Signup has a stored token and still cannot send until the number is
registered. The load-bearing behaviour: a mistyped PIN, Meta's rate-limit lock (133016) and an
unreachable Meta are three different sentences, never one indistinguishable 400.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
import app.core.whatsapp_register as wr


def _fake_post(status, body):
    return lambda url, payload, headers=None: (status, body)


def test_success_registers(monkeypatch):
    monkeypatch.setattr(wr, "post_json", _fake_post(200, '{"success": true}'))
    r = wr.register_number(access_token="t", phone_number_id="p", pin="123456", graph_base="")
    assert r.registered and r.reason == ""


def test_wrong_pin_is_named(monkeypatch):
    monkeypatch.setattr(wr, "post_json",
                        _fake_post(400, '{"error": {"code": 133005, "message": "PIN mismatch"}}'))
    r = wr.register_number(access_token="t", phone_number_id="p", pin="000000", graph_base="")
    assert not r.registered and r.reason == "WRONG_PIN"


def test_rate_limited_is_named_distinct_from_wrong_pin(monkeypatch):
    monkeypatch.setattr(wr, "post_json",
                        _fake_post(400, '{"error": {"code": 133016, "message": "too many attempts"}}'))
    r = wr.register_number(access_token="t", phone_number_id="p", pin="123456", graph_base="")
    assert not r.registered and r.reason == "RATE_LIMITED"


def test_unreachable_is_named(monkeypatch):
    # post_json returns (0, "request failed") on a network/DNS/TLS failure.
    monkeypatch.setattr(wr, "post_json", _fake_post(0, "request failed"))
    r = wr.register_number(access_token="t", phone_number_id="p", pin="123456", graph_base="")
    assert not r.registered and r.reason == "UNREACHABLE"


def test_other_meta_error_surfaces_its_message_but_stays_distinct(monkeypatch):
    monkeypatch.setattr(wr, "post_json",
                        _fake_post(400, '{"error": {"code": 100, "message": "bad param"}}'))
    r = wr.register_number(access_token="t", phone_number_id="p", pin="123456", graph_base="")
    assert not r.registered and r.reason.startswith("META_ERROR") and "bad param" in r.reason


# ── the route: token gate, PIN validation, distinct reasons over HTTP ─────────
ITOKEN = "i" * 40


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", ITOKEN)
    # config fallback gives the pool a token + phone id without a real Embedded Signup.
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "stored-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "PN1")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:      # context manager runs lifespan → provider_pool wired
        yield c
    get_settings.cache_clear()


def _hdr():
    return {"X-Internal-Token": ITOKEN}


def test_register_route_requires_the_internal_token(client):
    assert client.post("/internal/whatsapp/register",
                       json={"tenant": "acme", "pin": "123456"}).status_code == 401


def test_register_route_rejects_a_malformed_pin_before_spending_an_attempt(client):
    # a non-6-digit PIN is refused by validation (422) — it never reaches Meta, so it cannot burn one
    # of the 10 attempts.
    r = client.post("/internal/whatsapp/register",
                    json={"tenant": "acme", "pin": "12ab"}, headers=_hdr())
    assert r.status_code == 422


def test_register_route_returns_registered_true(client, monkeypatch):
    monkeypatch.setattr(wr, "post_json", _fake_post(200, "{}"))
    r = client.post("/internal/whatsapp/register",
                    json={"tenant": "acme", "pin": "123456"}, headers=_hdr())
    assert r.status_code == 200 and r.json() == {"registered": True}


def test_register_route_returns_the_distinct_reason(client, monkeypatch):
    monkeypatch.setattr(wr, "post_json",
                        _fake_post(400, '{"error": {"code": 133016}}'))
    r = client.post("/internal/whatsapp/register",
                    json={"tenant": "acme", "pin": "123456"}, headers=_hdr())
    assert r.status_code == 400 and r.json() == {"registered": False, "reason": "RATE_LIMITED"}


def test_register_route_says_not_connected_when_no_token_is_stored(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", ITOKEN)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")      # no stored credential
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        r = c.post("/internal/whatsapp/register",
                   json={"tenant": "fresh", "pin": "123456"}, headers=_hdr())
    get_settings.cache_clear()
    assert r.status_code == 400 and r.json()["reason"] == "NOT_CONNECTED"
