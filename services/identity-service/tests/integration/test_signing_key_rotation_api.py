"""End-to-end signing-key rotation over the running app: the JWKS publishes the current key, the
issuer stamps a matching kid, an internal rotation adds the new key while keeping the old one
verifiable, and enabling rotation without its prerequisites refuses to boot."""
from __future__ import annotations

import json
import os

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.signing_keyring import generate_rsa_keypair

KEK = "unit-test-kek-not-a-real-key"
ITOKEN = "t" * 40
_PRIV, _PUB = generate_rsa_keypair()


def _make_app(**env_over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "rs256",
        "TOKEN_PUBLIC_KEY": _PUB,
        "TOKEN_ISSUER_DRIVER": "rs256",
        "TOKEN_PRIVATE_KEY": _PRIV,
        "SECRETS_KEK": KEK,
        "PII_SALT": "s" * 32,
        "RATELIMIT_MAX": "1000",
        "TOKEN_KEY_ROTATION_ENABLED": "true",
    }
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings

    get_settings.cache_clear()


def _verify_against_jwks(token: str, jwks: dict) -> dict:
    hdr = jwt.get_unverified_header(token)
    jwk = next(k for k in jwks["keys"] if k["kid"] == hdr["kid"])
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    return jwt.decode(token, pub, algorithms=["RS256"], options={"verify_aud": False})


def test_jwks_publishes_current_key_and_issuer_stamps_a_matching_kid(env_guard):
    with _make_app() as client:                                  # `with` runs the lifespan
        jwks = client.get("/.well-known/jwks.json").json()
        assert len(jwks["keys"]) == 1 and jwks["keys"][0]["kid"]
        issuer = client.app.state.container.token_issuer
        token = issuer.issue_access("u1", {"caps": []}, 900)
        # the token verifies against the very key the JWKS advertises
        assert _verify_against_jwks(token, jwks)["sub"] == "u1"


def test_internal_rotate_adds_a_key_and_old_tokens_still_verify(env_guard):
    with _make_app() as client:
        issuer = client.app.state.container.token_issuer
        old_token = issuer.issue_access("before", {}, 900)
        old_kid = jwt.get_unverified_header(old_token)["kid"]

        r = client.post("/internal/signing-key/rotate?force=true",
                        headers={"X-Internal-Token": ITOKEN})
        assert r.status_code == 200 and r.json()["rotated"] is True
        new_kid = r.json()["kid"]
        assert new_kid != old_kid and r.json()["key_count"] == 2

        jwks = client.get("/.well-known/jwks.json").json()
        assert {k["kid"] for k in jwks["keys"]} == {old_kid, new_kid}
        # a token signed BEFORE the rotation still verifies (the overlap window is open)...
        assert _verify_against_jwks(old_token, jwks)["sub"] == "before"
        # ...and a token signed AFTER carries the new kid
        new_token = issuer.issue_access("after", {}, 900)
        assert jwt.get_unverified_header(new_token)["kid"] == new_kid
        assert _verify_against_jwks(new_token, jwks)["sub"] == "after"


def test_rotate_requires_the_internal_token(env_guard):
    with _make_app() as client:
        assert client.post("/internal/signing-key/rotate").status_code == 401
        assert client.post("/internal/signing-key/rotate",
                           headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_rotation_disabled_serves_empty_jwks_and_503s_the_rotate(env_guard):
    with _make_app(TOKEN_KEY_ROTATION_ENABLED="false") as client:
        assert client.get("/.well-known/jwks.json").json() == {"keys": []}
        r = client.post("/internal/signing-key/rotate", headers={"X-Internal-Token": ITOKEN})
        assert r.status_code == 503


def test_enabling_rotation_without_a_kek_refuses_to_boot(env_guard):
    with pytest.raises(ValueError):
        _make_app(SECRETS_KEK="")


def test_enabling_rotation_with_hs256_issuer_refuses_to_boot(env_guard):
    with pytest.raises(ValueError):
        _make_app(TOKEN_ISSUER_DRIVER="hs256", TOKEN_SIGNING_SECRET="x" * 40)
