"""POST /internal/whatsapp/webhook-override — the Meta-side handover lever (route a number's message
webhooks to an alternate endpoint). SECURITY: HTTPS + host allow-list (empty = fail closed), internal
token, audited critical, verify_token never returned. set/clear via the verified Meta shapes."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40
ALLOWED = "agent.meta.example"


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": "h" * 40, "RATELIMIT_MAX": "1000",
           "WHATSAPP_OVERRIDE_ALLOWED_HOSTS": ALLOWED}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


class _Pool:
    async def whatsapp_access_token(self, tenant): return "tok"
    async def whatsapp_phone_number_id(self, tenant): return "PN1"
    async def whatsapp_business_account_id(self, tenant): return "WABA1"


@pytest.fixture()
def app():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    c.app.state.provider_pool = _Pool()
    yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": ITOKEN}


# ── the pure allow-list guard ────────────────────────────────────────────────
def test_host_allowlist_guard():
    from app.api.internal import _override_host_allowed
    assert _override_host_allowed("https://agent.meta.example/hook", "agent.meta.example") is True
    assert _override_host_allowed("https://evil.example/hook", "agent.meta.example") is False   # wrong host
    assert _override_host_allowed("http://agent.meta.example/hook", "agent.meta.example") is False  # not https
    assert _override_host_allowed("https://agent.meta.example/h", "") is False                 # empty = deny
    assert _override_host_allowed("https://a.example/h", '["a.example","b.example"]') is True   # json list


def test_requires_internal_token(app):
    r = app.post("/internal/whatsapp/webhook-override",
                 json={"tenant": "acme", "callback_uri": f"https://{ALLOWED}/h", "verify_token": "v"})
    assert r.status_code == 401


def test_a_non_allowlisted_host_is_refused_422(app):
    r = app.post("/internal/whatsapp/webhook-override", headers=_tok(),
                 json={"tenant": "acme", "callback_uri": "https://evil.example/h", "verify_token": "v"})
    assert r.status_code == 422 and r.json()["code"] == "OVERRIDE_HOST_NOT_ALLOWED"


def test_http_target_is_refused(app):
    r = app.post("/internal/whatsapp/webhook-override", headers=_tok(),
                 json={"tenant": "acme", "callback_uri": f"http://{ALLOWED}/h", "verify_token": "v"})
    assert r.status_code == 422


def test_set_and_clear_call_meta_and_are_audited(app, monkeypatch):
    calls = {}
    from app.core import whatsapp_admin as wa

    def _set_phone(*, access_token, phone_number_id, callback_uri, verify_token, graph_base):
        calls["set"] = {"pid": phone_number_id, "uri": callback_uri, "vt": verify_token}
        return True, ""

    def _clear_phone(*, access_token, phone_number_id, graph_base):
        calls["clear"] = {"pid": phone_number_id}
        return True, ""

    monkeypatch.setattr(wa, "set_phone_webhook_override", _set_phone)
    monkeypatch.setattr(wa, "clear_phone_webhook_override", _clear_phone)

    r = app.post("/internal/whatsapp/webhook-override", headers=_tok(),
                 json={"tenant": "acme", "callback_uri": f"https://{ALLOWED}/hook",
                       "verify_token": "super-secret-token"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls["set"]["pid"] == "PN1" and calls["set"]["uri"] == f"https://{ALLOWED}/hook"
    # the verify_token must never be echoed back to the caller
    assert "super-secret-token" not in r.text

    r2 = app.post("/internal/whatsapp/webhook-override/clear", headers=_tok(),
                  json={"tenant": "acme"})
    assert r2.status_code == 200 and r2.json()["cleared"] is True and calls["clear"]["pid"] == "PN1"
