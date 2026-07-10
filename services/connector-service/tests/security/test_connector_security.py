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
