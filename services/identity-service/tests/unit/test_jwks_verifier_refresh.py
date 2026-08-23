"""The JWKS verifier forces ONE key-set refresh when it meets a kid it has not cached — the new key
from a rotation the cache predates. Without this, a rotated key would fail to verify fleet-wide for
up to a full cache lifespan; with it, the new key is picked up on first sight."""
from __future__ import annotations

import json

import jwt
import pytest

from app.adapters.token_verifier.jwks import JWKSVerifier
from app.core.signing_keyring import generate_rsa_keypair, public_jwk
from app.ports.errors import InvalidToken

_PRIV, _PUB = generate_rsa_keypair()
_JWK = public_jwk(_PUB)


class _FakeKey:
    def __init__(self, pem: str) -> None:
        self.key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(public_jwk(pem)))


class _FakeClient:
    """A PyJWKClient whose cache starts EMPTY — get_signing_key misses until a refresh populates it,
    exactly the state a verifier is in the moment the issuer rotates to a brand-new key."""

    def __init__(self) -> None:
        self.known: set[str] = set()
        self.refreshes = 0

    def get_signing_key(self, kid: str):
        if kid not in self.known:
            raise jwt.PyJWKClientError(f"no key {kid}")
        return _FakeKey(_PRIV)

    def get_jwk_set(self, refresh: bool = False):
        if refresh:
            self.refreshes += 1
            self.known.add(_JWK["kid"])          # the rotation's new key becomes visible on refresh


def _verifier_with(client) -> JWKSVerifier:
    v = JWKSVerifier("https://identity/.well-known/jwks.json")
    v._client = client                            # swap in the fake after construction
    return v


def test_unknown_kid_triggers_one_refresh_then_verifies():
    token = jwt.encode({"sub": "u", "exp": 9999999999}, _PRIV, algorithm="RS256",
                       headers={"kid": _JWK["kid"]})
    client = _FakeClient()
    v = _verifier_with(client)
    assert v.verify(token)["sub"] == "u"
    assert client.refreshes == 1                  # exactly one forced refresh, not a loop


def test_still_missing_after_refresh_is_an_invalid_token_not_a_crash():
    token = jwt.encode({"sub": "u", "exp": 9999999999}, _PRIV, algorithm="RS256",
                       headers={"kid": "never-published"})
    client = _FakeClient()
    with pytest.raises(InvalidToken):
        _verifier_with(client).verify(token)
    assert client.refreshes == 1                  # tried once, gave up — no unbounded refetch
