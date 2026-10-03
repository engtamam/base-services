"""Security: deny-by-default, credential never exposed, SSRF, secret-free errors, fail-fast."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="1000",
                      CONNECTOR_PROVIDER_DRIVER="recording", CREDENTIAL_SEAL_KEY="s" * 32)
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
    assert client.get("/api/v1/connectors").status_code == 401
    assert client.post("/api/v1/connectors/connect", json={"provider": "x"}).status_code == 401


def test_internal_sync_requires_token(client):
    assert client.post("/internal/connectors/sync",
                       json={"connection_id": "x", "resource": "y"}).status_code == 401


def test_connection_public_view_never_exposes_credentials():
    from app.core.models import Connection
    c = Connection(id="c", tenant="t", provider="p", access_sealed="SEALEDA", refresh_sealed="SEALEDR")
    pub = c.public()
    assert "access_sealed" not in pub and "refresh_sealed" not in pub and "SEALED" not in str(pub)


def test_spec_public_view_never_exposes_client_secret():
    from app.core.models import ProviderSpec
    s = ProviderSpec(id="s", tenant="t", name="n", client_secret_sealed="SEALEDSECRET")
    assert "SEALEDSECRET" not in str(s.public()) and s.public()["has_client_secret"] is True


def test_credentials_are_encrypted_at_rest():
    from app.core.seal import seal, unseal
    tok = seal("ya29.PLAINTEXT_TOKEN", "k" * 32)
    assert "PLAINTEXT" not in tok and unseal(tok, "k" * 32) == "ya29.PLAINTEXT_TOKEN"


def test_provider_errors_never_carry_the_token():
    import app.adapters.connector_http as H
    from app.ports.connector_provider import ProviderUnavailable
    try:
        H.get_json("https://192.0.2.1:9/never", {"Authorization": "Bearer supersecrettoken"})
        assert False, "should have raised"
    except ProviderUnavailable as e:
        assert "supersecrettoken" not in str(e)


def test_null_provider_fails_closed():
    from app.adapters.provider.null import NullConnector
    from app.ports.connector_provider import ProviderUnavailable
    with pytest.raises(ProviderUnavailable):
        NullConnector().exchange(code="x", redirect_uri="y", spec={})
    assert NullConnector().revoke(access_token="x", spec={}) is False


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 CONNECTOR_PROVIDER_DRIVER="carrier-pigeon"))
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 CLOCK_DRIVER="sundial"))


def test_seal_key_falls_back_so_creds_are_never_stored_in_the_clear():
    from app.config import Settings
    s = Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40, CREDENTIAL_SEAL_KEY="")
    assert s.seal_key == "t" * 40 and s.seal_key != ""


def test_connector_http_blocks_an_internal_target_at_call_time():
    """Every outbound fetch is SSRF-gated, not just spec registration — a metadata/internal address
    is refused even if it somehow got onto a spec (the DNS-rebind / bad-config case)."""
    import app.adapters.connector_http as H
    from app.ports.connector_provider import ProviderUnavailable
    H.configure(timeout_seconds=5, max_response_bytes=1024, require_https=False,
                allow_private=False, allowed_hosts=())
    for bad in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:8080/", "http://10.0.0.5/internal"):
        with pytest.raises(ProviderUnavailable):
            H.get_json(bad, {})


def test_token_and_revoke_urls_are_ssrf_validated_at_registration():
    # a spec pointing token_url at the cloud metadata endpoint is refused (not only base_url).
    from app.core import ssrf
    for url in ("http://169.254.169.254/token", "http://localhost/oauth"):
        with pytest.raises(ssrf.SsrfBlocked):
            ssrf.validate(url, require_https=False, allow_private=False, allowed_hosts=())


def _token(tenant: str) -> str:
    """An HS256 bearer for `tenant` with the connectors.manage capability (matches _make_app's secret)."""
    import time

    import jwt
    return jwt.encode({"sub": f"u@{tenant}", "tenant": tenant, "caps": ["connectors.manage"],
                       "exp": int(time.time()) + 3600},
                      "h" * 40, algorithm="HS256")


def test_connect_rejects_another_tenants_spec(client):
    """IDOR, exercised through the ACTUAL route (connector-011): shop_a registers a spec, shop_b tries to
    connect with that spec_id, and the endpoint must answer 404 SPEC_NOT_FOUND. Removing the guard in
    connect() turns this red — unlike the old test, which re-implemented the check and tested nothing."""
    a = {"Authorization": f"Bearer {_token('shop_a')}"}
    b = {"Authorization": f"Bearer {_token('shop_b')}"}

    made = client.post("/api/v1/connectors/specs",
                       json={"name": "hub", "provider": "recording"}, headers=a)
    assert made.status_code == 201, made.text
    spec_id = made.json()["id"]

    # shop_b cannot connect using shop_a's spec — it is not in shop_b's tenant.
    resp = client.post("/api/v1/connectors/connect",
                       json={"provider": "recording", "spec_id": spec_id,
                             "code": "x", "redirect_uri": "https://app.example/cb"}, headers=b)
    assert resp.status_code == 404      # the tenant guard rejects another tenant's spec_id

    # control: shop_a (the owner) passes the tenant guard — it does NOT 404 on the same spec_id.
    ok = client.post("/api/v1/connectors/connect",
                     json={"provider": "recording", "spec_id": spec_id,
                           "code": "x", "redirect_uri": "https://app.example/cb"}, headers=a)
    assert ok.status_code != 404
