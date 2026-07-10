"""Security: deny-by-default, fail-safe/fail-closed, no ReDoS, internal token required."""
from __future__ import annotations

import datetime as dt
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_deny_by_default(client):
    assert client.get("/api/v1/flags").status_code == 401
    assert client.post("/api/v1/flags", json={"key": "x"}).status_code == 401
    assert client.get("/api/v1/maintenance").status_code == 401


def test_internal_endpoints_require_token(client):
    assert client.post("/internal/flags/evaluate", json={"user": "a"}).status_code == 401
    assert client.get("/internal/flags/x").status_code == 401
    assert client.get("/internal/maintenance").status_code == 401
    assert client.get("/internal/config/x").status_code == 401


def test_no_regex_operator_exists():
    """User-supplied regex would be a ReDoS vector — the matcher allowlist has none."""
    from app.core.rules import _OPS
    assert "regex" not in _OPS and "matches" not in _OPS
    for op in _OPS:
        assert op in ("equals", "not_equals", "in", "not_in", "contains",
                      "prefix", "suffix", "gt", "gte", "lt", "lte")


def test_rules_never_evaluate_input():
    from app.core.rules import match_condition
    # a crafted "expression" is just an opaque string, never executed
    attrs = {"x": "__import__('os').system('echo pwn')"}
    assert match_condition({"attribute": "x", "op": "equals", "value": "safe"}, attrs) is False


def test_billing_entitlement_fails_closed_when_unreachable():
    from app.adapters.entitlement.billing_http import BillingHttpEntitlement
    assert BillingHttpEntitlement("", "").allowed(tenant="t", key="export") is False
    import app.adapters.entitlement.billing_http as B
    B.get_json = lambda url, headers=None: (0, {})       # network down
    assert BillingHttpEntitlement("http://billing", "tok").allowed(tenant="t", key="export") is False


def test_null_provider_never_turns_a_flag_on():
    import asyncio
    from app.adapters.flags.null import NullFlags
    assert asyncio.run(NullFlags().get("anything")) is None
    assert asyncio.run(NullFlags().list()) == []


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 FLAG_PROVIDER_DRIVER="wat"))
