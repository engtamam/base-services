"""POST /internal/whatsapp/two-step-pin — set/rotate the 6-digit PIN, write-only, token-gated.

The PIN is validated as exactly 6 digits BEFORE it can reach Meta (a malformed value must never spend
one of Meta's limited attempts), and the route never echoes the PIN back.
"""
from __future__ import annotations

import os

from app.config import get_settings


def _client(**extra):
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, **extra)
    get_settings.cache_clear()
    from starlette.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False), saved


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_requires_the_internal_token():
    c, saved = _client()
    try:
        assert c.post("/internal/whatsapp/two-step-pin",
                      json={"tenant": "t", "pin": "123456"}).status_code == 401
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_a_non_six_digit_pin_is_rejected_before_meta():
    c, saved = _client()
    try:
        for bad in ("12345", "1234567", "12ab56", ""):
            r = c.post("/internal/whatsapp/two-step-pin",
                       json={"tenant": "t", "pin": bad}, headers=_tok())
            assert r.status_code == 422, f"{bad!r} should be rejected"
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_response_never_echoes_the_pin(monkeypatch):
    c, saved = _client()
    try:
        # Stub the pool + Meta call so a well-formed request reaches the handler without real creds.
        import app.core.whatsapp_admin as wa
        monkeypatch.setattr(wa, "post_json", lambda u, p, h: (200, "{}"))

        class _Pool:
            async def whatsapp_access_token(self, tenant=""):
                return "tok"

            async def whatsapp_phone_number_id(self, tenant=""):
                return "PN1"
        c.app.state.provider_pool = _Pool()
        r = c.post("/internal/whatsapp/two-step-pin",
                   json={"tenant": "t", "pin": "654321"}, headers=_tok())
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert "654321" not in r.text                       # PIN never echoed
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()
