"""Non-security MEDIUM remediation (connector-service):
  006  connector_http refuses redirects (the opener is pinned; a Location must not be followed).
  007  an oversized response raises instead of truncating to {}, and a non-empty non-JSON body raises.
  008  TokenManager.bump surfaces a cache failure (telemetry + strict re-raise).
  012  Slack {"ok": false} is classified: auth vs rate-limit vs transient, not always CredentialInvalid.
"""
from __future__ import annotations

import asyncio
import io

import pytest

import app.adapters.connector_http as H
from app.adapters.provider.slack import SlackConnector
from app.core.token_manager import BumpFailed, TokenManager
from app.ports.connector_provider import (CredentialInvalid, ProviderUnavailable, RateLimited)


def run(c):
    return asyncio.run(c)


# ── 007: size cap + strict parse ──────────────────────────────────────────────
def test_parse_empty_body_is_empty_dict():
    assert H._parse(b"") == {}
    assert H._parse(b"", strict=True) == {}


def test_parse_non_json_strict_raises_not_silently_empty():
    with pytest.raises(ProviderUnavailable):
        H._parse(b"<html>nope</html>", strict=True)
    assert H._parse(b"<html>nope</html>") == {}            # lenient path stays best-effort


def test_read_capped_raises_on_oversize():
    old = H.MAX_BYTES
    H.MAX_BYTES = 16
    try:
        with pytest.raises(H.ResponseTooLarge):
            H._read_capped(io.BytesIO(b"x" * 1000))
        assert H._read_capped(io.BytesIO(b"small")) == b"small"
    finally:
        H.MAX_BYTES = old


# ── 006: redirects are refused ────────────────────────────────────────────────
def test_opener_installs_a_no_redirect_handler():
    target = H._ssrf.Target(url="https://example.com/", ip="93.184.216.34", host="example.com", port=443)
    opener = H._pinned_opener(target)
    # the redirect handler must raise rather than follow a Location
    redirect_handlers = [h for h in opener.handlers if isinstance(h, H.urllib.request.HTTPRedirectHandler)]
    assert redirect_handlers
    with pytest.raises(H._RedirectRefused):
        redirect_handlers[0].redirect_request(None, None, 302, "Found", {}, "https://evil.example/")


# ── 008: bump surfaces a cache failure ────────────────────────────────────────
class _BrokenCache:
    async def get(self, k):
        raise RuntimeError("cache down")

    async def set(self, k, v, ttl):
        raise RuntimeError("cache down")


class _Telemetry:
    def __init__(self): self.events = []
    def event(self, name, data): self.events.append((name, data))


def test_bump_strict_raises_and_telemeters_on_cache_failure():
    tel = _Telemetry()
    tm = TokenManager(connections=None, provider=None, clock=None, seal_key="k" * 32,
                      cache=_BrokenCache(), telemetry=tel)
    with pytest.raises(BumpFailed):
        run(tm.bump("t1", strict=True))
    assert any(n == "connection.bump_failed" for n, _ in tel.events)


def test_bump_non_strict_swallows_but_still_telemeters():
    tel = _Telemetry()
    tm = TokenManager(connections=None, provider=None, clock=None, seal_key="k" * 32,
                      cache=_BrokenCache(), telemetry=tel)
    run(tm.bump("t1"))                                      # no raise
    assert any(n == "connection.bump_failed" for n, _ in tel.events)


# ── 012: Slack error classification ───────────────────────────────────────────
def _slack_with_body(body, headers=None):
    sc = SlackConnector()

    def fake_get_json(url, h):
        return 200, body, headers or {}

    return sc, fake_get_json


def test_slack_auth_error_is_credential_invalid(monkeypatch):
    sc, fake = _slack_with_body({"ok": False, "error": "invalid_auth"})
    monkeypatch.setattr("app.adapters.provider.slack.get_json", fake)
    with pytest.raises(CredentialInvalid):
        sc.fetch(access_token="t", resource="conversations.list", cursor="", limit=100, spec={})


def test_slack_ratelimited_is_rate_limited_with_retry_after(monkeypatch):
    sc, fake = _slack_with_body({"ok": False, "error": "ratelimited"}, {"Retry-After": "30"})
    monkeypatch.setattr("app.adapters.provider.slack.get_json", fake)
    with pytest.raises(RateLimited) as ei:
        sc.fetch(access_token="t", resource="conversations.list", cursor="", limit=100, spec={})
    assert ei.value.retry_after == 30.0


def test_slack_transient_error_is_provider_unavailable_not_credential(monkeypatch):
    sc, fake = _slack_with_body({"ok": False, "error": "internal_error"})
    monkeypatch.setattr("app.adapters.provider.slack.get_json", fake)
    with pytest.raises(ProviderUnavailable):
        sc.fetch(access_token="t", resource="conversations.list", cursor="", limit=100, spec={})


def test_slack_unknown_error_is_provider_unavailable_not_credential(monkeypatch):
    # An unknown per-call error must NOT condemn the credential (NEEDS_REAUTH) on a pull.
    sc, fake = _slack_with_body({"ok": False, "error": "channel_not_found"})
    monkeypatch.setattr("app.adapters.provider.slack.get_json", fake)
    with pytest.raises(ProviderUnavailable):
        sc.fetch(access_token="t", resource="conversations.list", cursor="", limit=100, spec={})
