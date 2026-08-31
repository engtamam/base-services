"""The HTTP wallet + channel-pricing clients — do they send wallet-service/operations the request they
expect, and read the money-critical distinctions back correctly (402 == refuse, other non-2xx == the
posture's call, a 2xx with no hold_id is NOT a silent success)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.channel_pricing import http as pricing_mod
from app.adapters.channel_pricing.http import HttpChannelPricing
from app.adapters.wallet import http as wallet_mod
from app.adapters.wallet.http import HttpWallet
from app.ports.wallet import INSUFFICIENT, UNAVAILABLE


def _capture(mod, response):
    sent = {}

    def fake_post_json(url, payload, headers):
        sent["url"], sent["payload"], sent["headers"] = url, payload, headers
        return response

    mod.post_json = fake_post_json
    return sent


# ── wallet ───────────────────────────────────────────────────────────────────────
def test_reserve_sends_net_and_reads_the_hold_id(monkeypatch):
    sent = _capture(wallet_mod, (200, json.dumps({"hold_id": "H9", "expires_at": 1}))
                    )
    r = asyncio.run(HttpWallet("http://wallet:8000", "tok").reserve(
        tenant="acme", amount=7, source="wa:acme:rid", reason="whatsapp_overage"))
    assert r.ok and r.hold_id == "H9"
    assert sent["url"] == "http://wallet:8000/wallet/reserve"
    assert sent["payload"] == {"tenant": "acme", "net": 7, "source": "wa:acme:rid",
                               "reason": "whatsapp_overage"}
    assert sent["headers"]["X-Internal-Token"] == "tok"


def test_reserve_402_is_insufficient(monkeypatch):
    _capture(wallet_mod, (402, json.dumps({"detail": "INSUFFICIENT_FUNDS"})))
    r = asyncio.run(HttpWallet("http://w", "t").reserve(tenant="a", amount=5, source="s", reason="r"))
    assert not r.ok and r.refused == INSUFFICIENT


def test_reserve_other_error_is_unavailable(monkeypatch):
    _capture(wallet_mod, (503, "down"))
    r = asyncio.run(HttpWallet("http://w", "t").reserve(tenant="a", amount=5, source="s", reason="r"))
    assert r.refused == UNAVAILABLE


def test_reserve_2xx_without_hold_id_is_not_a_silent_success(monkeypatch):
    # a broken contract must NOT look like a reservation we can later confirm/release.
    _capture(wallet_mod, (200, json.dumps({"expires_at": 1})))
    r = asyncio.run(HttpWallet("http://w", "t").reserve(tenant="a", amount=5, source="s", reason="r"))
    assert not r.ok and r.refused == UNAVAILABLE


def test_confirm_and_release_post_the_hold_id(monkeypatch):
    sent = _capture(wallet_mod, (200, "{}"))
    assert asyncio.run(HttpWallet("http://w", "t").confirm(hold_id="H1")) is True
    assert sent["payload"] == {"hold_id": "H1"} and sent["url"].endswith("/wallet/confirm")
    assert asyncio.run(HttpWallet("http://w", "t").release(hold_id="H1")) is True
    assert sent["url"].endswith("/wallet/release")


# ── pricing ────────────────────────────────────────────────────────────────────────
def test_pricing_posts_tenant_and_parses_policy(monkeypatch):
    sent = _capture(pricing_mod, (200, json.dumps(
        {"included_messages_per_period": 100, "overage_halalas_per_message": 3})))
    p = HttpChannelPricing("http://operations:8000", "tok", ttl_seconds=300, clock=lambda: 1000.0)
    policy = asyncio.run(p.get("acme"))
    assert policy == {"included_messages_per_period": 100, "overage_halalas_per_message": 3}
    assert sent["url"] == "http://operations:8000/internal/operations/whatsapp-channel-pricing"
    assert sent["payload"] == {"tenant": "acme"}
    assert sent["headers"]["X-Internal-Token"] == "tok"


def test_pricing_defaults_missing_fields_to_zero(monkeypatch):
    _capture(pricing_mod, (200, json.dumps({})))
    policy = asyncio.run(HttpChannelPricing("http://o", "t").get("acme"))
    assert policy == {"included_messages_per_period": 0, "overage_halalas_per_message": 0}


def test_pricing_unreachable_returns_none(monkeypatch):
    _capture(pricing_mod, (502, "bad gateway"))
    assert asyncio.run(HttpChannelPricing("http://o", "t").get("acme")) is None


def test_pricing_caches_within_ttl(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def fake_post_json(url, payload, headers):
        calls["n"] += 1
        return (200, json.dumps({"included_messages_per_period": 5, "overage_halalas_per_message": 1}))

    pricing_mod.post_json = fake_post_json
    p = HttpChannelPricing("http://o", "t", ttl_seconds=300, clock=lambda: clock["t"])
    assert asyncio.run(p.get("acme"))["included_messages_per_period"] == 5
    assert asyncio.run(p.get("acme"))["included_messages_per_period"] == 5     # cache hit
    assert calls["n"] == 1
    clock["t"] += 301                                                          # past TTL
    asyncio.run(p.get("acme"))
    assert calls["n"] == 2                                                     # refetched
