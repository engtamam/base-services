"""Phase 2 — the marketplace registry + resolver (provider-neutral construction, fail-closed)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.payment import MARKETPLACE_DRIVERS, build_marketplace_gateway
from app.adapters.payment.moyasar import MoyasarGateway
from app.adapters.payment.tap import TapGateway
from app.core.marketplace_credentials import (PROVIDER, SECRET_KEY, MarketplaceCredentials,
                                             MarketplaceGateways, marketplace_secret_store)
from app.ports.payment_gateway import MarketplaceGateway


def run(c):
    return asyncio.run(c)


def test_registry_lists_both_providers():
    assert set(MARKETPLACE_DRIVERS) == {"tap", "moyasar"}


def test_build_tap():
    assert isinstance(build_marketplace_gateway("tap", {"api_key": "sk"}), TapGateway)


def test_build_moyasar():
    assert isinstance(build_marketplace_gateway("moyasar", {"api_key": "sk"}), MoyasarGateway)


def test_build_unknown_is_none():
    assert build_marketplace_gateway("stripe", {"api_key": "sk"}) is None
    assert build_marketplace_gateway("", {}) is None


def test_built_adapters_are_marketplace_gateways():
    assert isinstance(build_marketplace_gateway("tap", {"api_key": "sk"}), MarketplaceGateway)
    assert isinstance(build_marketplace_gateway("moyasar", {"api_key": "sk"}), MarketplaceGateway)


def _resolver():
    ds = MemoryDataStore()
    s = marketplace_secret_store(ds, "k" * 40)
    return MarketplaceGateways(MarketplaceCredentials(s)), s


def test_resolver_unconfigured_is_fail_closed():
    r, _ = _resolver()
    assert run(r.configured()) is False
    assert run(r.gateway()) is None               # no provider/key → no gateway


def test_resolver_builds_the_configured_provider():
    r, s = _resolver()
    run(s.set(tenant="", name=PROVIDER, value="moyasar"))
    run(s.set(tenant="", name=SECRET_KEY, value="sk_live"))
    assert run(r.configured()) is True
    gw = run(r.gateway())
    assert isinstance(gw, MoyasarGateway)


def test_resolver_unknown_provider_is_none():
    r, s = _resolver()
    run(s.set(tenant="", name=PROVIDER, value="paypal"))
    run(s.set(tenant="", name=SECRET_KEY, value="sk"))
    assert run(r.gateway()) is None               # unknown provider → fail-closed None


def test_resolver_provider_without_key_is_none():
    r, s = _resolver()
    run(s.set(tenant="", name=PROVIDER, value="tap"))
    assert run(r.gateway()) is None               # provider but no secret key → None
