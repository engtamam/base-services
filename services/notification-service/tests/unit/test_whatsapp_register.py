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


def test_a_revoked_token_is_its_own_reason_not_meta_error(monkeypatch):
    # Item 7: a revoked/expired token (Graph 190 / OAuthException) is the ONE failure that means
    # "stop and re-connect" — it must not be swallowed into the generic META_ERROR bucket that also
    # holds rate limits and wrong PINs, or a disconnect sweep can never act on it.
    for body in ('{"error": {"code": 190, "message": "expired"}}',
                 '{"error": {"type": "OAuthException", "message": "invalid"}}'):
        monkeypatch.setattr(wr, "post_json", _fake_post(401, body))
        r = wr.register_number(access_token="t", phone_number_id="p", pin="123456", graph_base="")
        assert r.reason == "TOKEN_REVOKED"


def test_status_reports_the_registration_status_field(monkeypatch):
    # Item 6: `status` (CONNECTED == registered and able to send) is the field the screen needs —
    # code_verification_status only says the two-step code was verified, which a number can pass and
    # still refuse every message.
    monkeypatch.setattr(wr, "get_json", lambda url, params=None, headers=None: (
        200, '{"status": "CONNECTED", "code_verification_status": "VERIFIED"}'))
    # the field list asks Meta for `status`
    called = {}
    monkeypatch.setattr(wr, "get_json", lambda url, params=None, headers=None: called.update(params or {}) or (
        200, '{"status": "CONNECTED", "code_verification_status": "VERIFIED"}'))
    out = wr.fetch_status(access_token="t", phone_number_id="p", graph_base="")
    assert "status" in called["fields"]
    assert out["status"] == "CONNECTED" and out["registered"] is True


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


def test_register_route_is_per_number(client, monkeypatch):
    # a chain has many numbers under one WABA — the caller names which number to register, and it is
    # the one registered (not the tenant's default).
    captured = {}

    def _capture(url, payload, headers=None):
        captured["url"] = url
        return (200, "{}")

    monkeypatch.setattr(wr, "post_json", _capture)
    r = client.post("/internal/whatsapp/register",
                    json={"tenant": "chain", "phone_number_id": "BRANCH_7", "pin": "123456"},
                    headers=_hdr())
    assert r.status_code == 200 and r.json() == {"registered": True}
    assert "/BRANCH_7/register" in captured["url"]      # the SPECIFIED number, not the tenant default


def test_register_route_says_no_pin_when_none_supplied_and_none_sealed(client, monkeypatch):
    # PIN omitted and nothing sealed for this number → a DISTINCT NO_PIN, never a Meta call.
    called = {"n": 0}
    monkeypatch.setattr(wr, "post_json",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (200, "{}"))
    r = client.post("/internal/whatsapp/register",
                    json={"tenant": "acme", "phone_number_id": "NEW1"}, headers=_hdr())
    # 409, matching /whatsapp/status: a missing precondition (no PIN available) is a state conflict,
    # not a malformed request. The two sibling endpoints must agree so no consumer special-cases one.
    assert r.status_code == 409 and r.json()["reason"] == "NO_PIN"
    assert called["n"] == 0                              # never reached Meta


# ── the readiness read ────────────────────────────────────────────────────────
def test_status_reads_the_numbers_real_state(client, monkeypatch):
    monkeypatch.setattr(
        wr, "get_json",
        lambda url, params=None, headers=None: (200, '{"platform_type": "CLOUD_API", '
        '"code_verification_status": "VERIFIED", "quality_rating": "GREEN", "throughput": {"level": "STANDARD"}}'))
    r = client.get("/internal/whatsapp/status", params={"tenant": "acme"}, headers=_hdr())
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] and b["platform_type"] == "CLOUD_API" and b["quality_rating"] == "GREEN"


def test_status_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/status", params={"tenant": "acme"}).status_code == 401


def test_status_refusal_is_non_2xx_not_a_200_with_ok_false(client, monkeypatch):
    # Item 5: one rule across the internal API — a refusal is a non-2xx, never {ok:false} on a 200 a
    # status-code-only caller reads as success. NOT_CONNECTED -> 409; a failed Meta read -> 502.
    import app.core.whatsapp_register as wrmod
    # no creds for this tenant -> NOT_CONNECTED must be 409
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        r = c.get("/internal/whatsapp/status", params={"tenant": "fresh"}, headers=_hdr())
        assert r.status_code == 409 and r.json()["reason"] == "NOT_CONNECTED"
    get_settings.cache_clear()


def test_status_failed_meta_read_is_502(client, monkeypatch):
    import app.core.whatsapp_register as wrmod
    monkeypatch.setattr(wrmod, "get_json",
                        lambda url, params=None, headers=None: (401, '{"error":{"code":190}}'))
    r = client.get("/internal/whatsapp/status", params={"tenant": "acme"}, headers=_hdr())
    assert r.status_code == 502 and r.json()["reason"] == "TOKEN_REVOKED"


# ── item 1: per-branch sending identity ───────────────────────────────────────
def test_notify_refuses_an_unknown_sending_identity(client):
    # a message from the wrong branch's number is worse than one not sent: an unknown identity is a
    # 409, never a silent fall-back to the tenant default.
    r = client.post("/internal/notify", headers=_hdr(), json={
        "recipient": "+966500000000", "channel": "whatsapp", "template_key": "otp",
        "tenant": "acme", "sending_identity": "NOT_MY_NUMBER"})
    assert r.status_code == 409 and r.json()["reason"] == "UNKNOWN_SENDING_IDENTITY"


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
    # 409, the SAME code /whatsapp/status returns for NOT_CONNECTED — a shop that has not finished
    # Embedded Signup is a state conflict, not a bad request. The two used to disagree (400 vs 409).
    assert r.status_code == 409 and r.json()["reason"] == "NOT_CONNECTED"
