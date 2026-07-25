"""CORS — the rule no server-side test was catching.

There was no CORS anywhere in this platform. Every request from a terminal worked, and every
fetch() from a client app or an operator panel was refused by the browser before our code saw it.
That gap is invisible to ordinary tests: the browser enforces the rule, so a test that calls the
app directly passes whether the headers are there or not.

These tests assert on the HEADERS, which is the only part of the contract that lives on our side.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

ORIGIN = "https://app.example.com"


def _client(**over):
    """A fresh app with CORS configured.

    Through the ENVIRONMENT, not by injecting a Settings object. An earlier version of this file
    swapped `get_settings.__wrapped__`, which never reached create_app — the app read the cached
    singleton and the tests asserted against a configuration it had never seen. Three of them
    failed and were telling the truth; the harness was the lie.
    """
    import os

    from app.config import get_settings
    from app.main import create_app

    env = dict(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="test", PII_SALT="s" * 32,
               **{k: str(v) for k, v in over.items()})
    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        get_settings.cache_clear()
        return TestClient(create_app())
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def test_no_origins_configured_emits_no_cors_headers():
    """The default is closed, and closed means SILENT: no header at all, so a browser refuses.

    An empty allow-list that emitted `Access-Control-Allow-Origin: ` would be a different and
    worse thing — a header that looks configured and permits nobody.
    """
    r = _client().get("/internal/health", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_a_configured_origin_is_allowed():
    r = _client(CORS_ALLOWED_ORIGINS=ORIGIN).get("/internal/health", headers={"Origin": ORIGIN})
    assert r.headers.get("access-control-allow-origin") == ORIGIN


def test_an_unlisted_origin_is_not_allowed():
    """The whole point: a list that admits everybody is not a list."""
    r = _client(CORS_ALLOWED_ORIGINS=ORIGIN).get(
        "/internal/health", headers={"Origin": "https://evil.example.com"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_credentials_are_off_unless_asked_for():
    r = _client(CORS_ALLOWED_ORIGINS=ORIGIN).get("/internal/health", headers={"Origin": ORIGIN})
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}


# ── the boot guard ────────────────────────────────────────────────────────────
def _settings(**over):
    base = dict(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production", PII_SALT="s" * 32)
    return Settings(**{**base, **over})


def test_wildcard_with_credentials_refuses_to_start():
    """Browsers refuse this combination, so it would emit headers that satisfy nobody — a
    configuration and a symptom that disagree, which is the worst thing to debug."""
    with pytest.raises(ValueError, match="CORS_ALLOW_CREDENTIALS"):
        _settings(CORS_ALLOWED_ORIGINS="*", CORS_ALLOW_CREDENTIALS=True).assert_cors_config_ok()


def test_wildcard_in_production_refuses_to_start():
    with pytest.raises(ValueError, match="production"):
        _settings(CORS_ALLOWED_ORIGINS="*").assert_cors_config_ok()


def test_a_real_origin_list_is_accepted_in_production():
    _settings(CORS_ALLOWED_ORIGINS=f"{ORIGIN},https://admin.example.com").assert_cors_config_ok()


def test_the_empty_default_is_accepted():
    """Shipping closed must not mean shipping unstartable."""
    _settings().assert_cors_config_ok()


def test_origins_are_parsed_and_trimmed():
    s = _settings(CORS_ALLOWED_ORIGINS=f" {ORIGIN} , https://b.example.com ,, ")
    assert s.cors_origins == [ORIGIN, "https://b.example.com"]
