"""VertexTokenSource — mint + cache a GCP OAuth token for Vertex AI, no vendor SDK, no network.

The safety points: a service-account key is signed into a correct JWT and exchanged for a token;
the token is cached and reused until just before expiry; the metadata-server path works with no key;
and bad config fails clearly rather than silently sending an empty Bearer.
"""
from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from app.core.gcp_token import VertexTokenSource
from app.ports.errors import ConfigError


def run(c):
    return asyncio.run(c)


def _sa_json() -> tuple[str, str]:
    """A throwaway RSA key as a service-account JSON. Returns (json, public_pem) for verification."""
    import json
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    sa = {"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": priv,
          "token_uri": "https://oauth2.googleapis.test/token", "private_key_id": "kid1"}
    return json.dumps(sa), pub


def test_service_account_signs_a_valid_jwt_and_returns_the_token():
    sa_json, pub = _sa_json()
    seen = {}

    async def transport(method, url, headers, data):
        seen.update(method=method, url=url, data=data)
        # decode the assertion the source sent, verifying it against the SA public key
        assertion = data.split("assertion=", 1)[1]
        claims = jwt.decode(assertion, pub, algorithms=["RS256"],
                            audience="https://oauth2.googleapis.test/token")
        seen["claims"] = claims
        return 200, {"access_token": "ya29.TOKEN", "expires_in": 3600}

    src = VertexTokenSource(sa_json=sa_json, transport=transport)
    assert run(src.token()) == "ya29.TOKEN"
    assert seen["method"] == "POST" and seen["url"] == "https://oauth2.googleapis.test/token"
    assert seen["claims"]["iss"] == "svc@proj.iam.gserviceaccount.com"
    assert seen["claims"]["scope"] == "https://www.googleapis.com/auth/cloud-platform"


def test_token_is_cached_until_near_expiry():
    sa_json, _ = _sa_json()
    calls = {"n": 0}
    clock = {"t": 1000.0}

    async def transport(method, url, headers, data):
        calls["n"] += 1
        return 200, {"access_token": f"tok{calls['n']}", "expires_in": 3600}

    src = VertexTokenSource(sa_json=sa_json, transport=transport, now=lambda: clock["t"])
    assert run(src.token()) == "tok1"
    clock["t"] += 100                      # still well inside the hour
    assert run(src.token()) == "tok1" and calls["n"] == 1     # reused, not re-minted
    clock["t"] += 3600                     # now past expiry (minus skew)
    assert run(src.token()) == "tok2" and calls["n"] == 2     # refreshed


def test_metadata_server_path_needs_no_key():
    async def transport(method, url, headers, data):
        assert method == "GET" and "metadata.google.internal" in url
        assert headers.get("Metadata-Flavor") == "Google"
        return 200, {"access_token": "metadata-tok", "expires_in": 3600}

    src = VertexTokenSource(sa_json="", transport=transport)     # no key → metadata mode
    assert run(src.token()) == "metadata-tok"


def test_bad_sa_json_is_a_clear_config_error():
    with pytest.raises(ConfigError):
        VertexTokenSource(sa_json="{not json")
    with pytest.raises(ConfigError):
        VertexTokenSource(sa_json='{"client_email":"a"}')       # missing private_key/token_uri


def test_a_failed_token_exchange_raises_not_a_blank_bearer():
    sa_json, _ = _sa_json()

    async def transport(method, url, headers, data):
        return 401, {"error": "invalid_grant"}

    with pytest.raises(ConfigError):
        run(VertexTokenSource(sa_json=sa_json, transport=transport).token())
