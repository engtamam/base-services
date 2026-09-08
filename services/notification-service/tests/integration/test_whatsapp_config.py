"""GET /internal/whatsapp/config — the non-secret Embedded-Signup client config for a product console.

Service-token auth, platform-wide, and it must NEVER leak the app secret; `configured` reflects only
whether both the app id and the config id are present.
"""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings


def _client(**extra):
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, **extra)
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app())
    return c, saved


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_requires_the_internal_token():
    c, saved = _client(META_APP_ID="123", META_APP_CONFIG_ID="cfg")
    try:
        assert c.get("/internal/whatsapp/config").status_code == 401
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_returns_config_and_never_the_secret():
    c, saved = _client(META_APP_ID="app123", META_APP_CONFIG_ID="cfg456",
                       META_APP_SECRET="topsecret", WHATSAPP_GRAPH_BASE="https://graph.facebook.com/v26.0")
    try:
        body = c.get("/internal/whatsapp/config", headers=_tok()).json()
        assert body == {"app_id": "app123", "config_id": "cfg456",
                        "graph_version": "v26.0", "configured": True}
        assert "topsecret" not in str(body) and "secret" not in body
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_not_configured_when_config_id_missing():
    c, saved = _client(META_APP_ID="app123")     # no config id
    try:
        body = c.get("/internal/whatsapp/config", headers=_tok()).json()
        assert body["app_id"] == "app123" and body["config_id"] == ""
        assert body["configured"] is False
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()
