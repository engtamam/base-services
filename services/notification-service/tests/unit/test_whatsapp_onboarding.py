"""Embedded Signup exchange — the token reaches the sealed store and NOWHERE else, the flow fits the
30-second budget by doing the exchange in-request, and a dead code fails cleanly."""
from __future__ import annotations

import asyncio

import pytest

import app.core.whatsapp_onboarding as wo
from app.core.whatsapp_onboarding import OnboardingError, WhatsAppOnboarding
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


class _Store:
    """A stand-in secret store recording sealed writes (value kept only so the test can assert it
    was stored — the real store seals it)."""
    def __init__(self):
        self.saved = {}

    async def set(self, *, tenant, name, value, actor_ref=""):
        self.saved[(tenant, name)] = value
        return None


def _patch_http(monkeypatch, *, exchange=(200, '{"access_token":"BShopToken","token_type":"bearer"}'),
                subscribe=(200, '{"success":true}')):
    calls = {"exchange": None, "subscribe": None}

    def fake_get(url, params=None, headers=None):
        calls["exchange"] = {"url": url, "params": params}
        return exchange

    def fake_post(url, payload=None, headers=None):
        calls["subscribe"] = {"url": url, "headers": headers}
        return subscribe

    monkeypatch.setattr(wo, "get_json", fake_get)
    monkeypatch.setattr(wo, "post_json", fake_post)
    return calls


def _svc(store, **kw):
    return WhatsAppOnboarding(store, app_id="APPID", app_secret="APPSECRET",
                              graph_base="https://graph.facebook.com/v21.0", **kw)


def test_happy_path_stores_all_three_credentials_and_subscribes(monkeypatch):
    calls = _patch_http(monkeypatch)
    store = _Store()
    seen = []
    svc = _svc(store, on_stored=lambda t: seen.append(t))
    res = run(svc.exchange(tenant="acme", code="CODE30S", waba_id="W1", phone_number_id="P1"))

    assert res == {"ok": True, "subscribed": True}
    assert store.saved[("acme", "WHATSAPP_ACCESS_TOKEN")] == "BShopToken"
    assert store.saved[("acme", "WHATSAPP_PHONE_NUMBER_ID")] == "P1"
    assert store.saved[("acme", "WHATSAPP_BUSINESS_ACCOUNT_ID")] == "W1"
    assert seen == ["acme"]                                   # pool cache invalidated for the tenant
    # exchange used OUR app credentials + the code, against oauth/access_token
    assert calls["exchange"]["params"] == {"client_id": "APPID", "client_secret": "APPSECRET", "code": "CODE30S"}
    assert calls["exchange"]["url"].endswith("/oauth/access_token")
    # subscribe carried the business token as a Bearer, against the WABA
    assert calls["subscribe"]["url"].endswith("/W1/subscribed_apps")
    assert calls["subscribe"]["headers"]["Authorization"] == "Bearer BShopToken"


def test_the_token_is_never_in_the_result(monkeypatch):
    _patch_http(monkeypatch)
    res = run(_svc(_Store()).exchange(tenant="acme", code="c", waba_id="W1", phone_number_id="P1"))
    assert "BShopToken" not in str(res) and "access_token" not in res


def test_an_expired_or_invalid_code_fails_cleanly_and_stores_nothing(monkeypatch):
    _patch_http(monkeypatch, exchange=(400, '{"error":{"message":"Invalid verification code."}}'))
    store = _Store()
    with pytest.raises(OnboardingError) as e:
        run(_svc(store).exchange(tenant="acme", code="dead", waba_id="W1", phone_number_id="P1"))
    assert "Meta rejected" in str(e.value)
    assert store.saved == {}                                  # nothing stored on failure


def test_a_failed_subscribe_is_nonfatal_but_reported(monkeypatch):
    _patch_http(monkeypatch, subscribe=(500, "boom"))
    store = _Store()
    res = run(_svc(store).exchange(tenant="acme", code="c", waba_id="W1", phone_number_id="P1"))
    assert res["ok"] is True and res["subscribed"] is False   # token still stored, subscribe retryable
    assert store.saved[("acme", "WHATSAPP_ACCESS_TOKEN")] == "BShopToken"


def test_refuses_when_not_a_tech_provider(monkeypatch):
    _patch_http(monkeypatch)
    svc = WhatsAppOnboarding(_Store(), app_id="", app_secret="", graph_base="x")
    with pytest.raises(ConfigError):
        run(svc.exchange(tenant="acme", code="c", waba_id="W1", phone_number_id="P1"))


def test_missing_identifiers_are_refused(monkeypatch):
    _patch_http(monkeypatch)
    with pytest.raises(OnboardingError):
        run(_svc(_Store()).exchange(tenant="acme", code="c", waba_id="", phone_number_id="P1"))
