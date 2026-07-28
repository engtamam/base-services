"""Runtime, per-tenant payment-gateway switching — and its fail-closed direction.

The load-bearing test: switching a tenant to a driver that is NOT configured must never fall back to
the boot provider (that would charge the wrong gateway) — it fails closed, so the charge is retryable
by dunning rather than going somewhere it should not.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.container import _UnconfiguredGateway, rebuild_gateway
from app.ports.payment_gateway import GatewayUnavailable


def _settings(**over):
    return Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40, **over)


_SENTINEL = object()


def test_same_driver_no_cred_change_keeps_the_boot_gateway():
    s = _settings(PAYMENT_GATEWAY_DRIVER="null")
    assert rebuild_gateway(s, {}, _SENTINEL, driver="null") is _SENTINEL


def test_switch_to_an_unknown_driver_fails_closed_not_boot():
    s = _settings(PAYMENT_GATEWAY_DRIVER="null")
    gw = rebuild_gateway(s, {}, _SENTINEL, driver="does-not-exist")
    assert isinstance(gw, _UnconfiguredGateway) and gw is not _SENTINEL
    with pytest.raises(GatewayUnavailable):
        gw.charge(amount_minor=100, currency="SAR", token="t", idempotency_key="k")


def test_switch_to_a_known_driver_with_no_keys_fails_closed():
    # hyperpay requires api_key + entityId; switching to it without them must not charge via boot.
    s = _settings(PAYMENT_GATEWAY_DRIVER="null")
    gw = rebuild_gateway(s, {}, _SENTINEL, driver="hyperpay")
    assert isinstance(gw, _UnconfiguredGateway)
    with pytest.raises(GatewayUnavailable):
        gw.refund(provider_id="p", currency="SAR")


def test_switch_to_a_configured_driver_builds_that_provider():
    s = _settings(PAYMENT_GATEWAY_DRIVER="null")
    # the tenant's resolved credentials for hyperpay, laid over settings by rebuild_gateway
    resolved = {"PAYMENT_API_KEY": "k", "PAYMENT_EXTRA_ID": "ent1"}
    gw = rebuild_gateway(s, resolved, _SENTINEL, driver="hyperpay")
    assert getattr(gw, "driver", "") == "hyperpay"


def test_same_driver_rotated_credentials_rebuilds_that_provider():
    # The existing per-tenant credential path still works: a rotated key on the SAME driver rebuilds
    # the same provider (a new object with the tenant's key), never the fail-closed sentinel.
    s = _settings(PAYMENT_GATEWAY_DRIVER="hyperpay", PAYMENT_API_KEY="boot", PAYMENT_EXTRA_ID="e")
    gw = rebuild_gateway(s, {"PAYMENT_API_KEY": "tenant-key"}, _SENTINEL, driver="hyperpay")
    assert getattr(gw, "driver", "") == "hyperpay" and not isinstance(gw, _UnconfiguredGateway)
