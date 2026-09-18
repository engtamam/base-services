"""The internal caller-identity seam: shared token, RS256 assertions (static keys), per-route policy,
and the mint→verify round-trip. These are the load-bearing checks for a change that will guard every
service's /internal tier fleet-wide.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.internal_caller.assertion import AssertionCaller, StaticKeyResolver
from app.adapters.internal_caller.shared import SharedTokenCaller
from app.core.internal_assertion import mint_internal_assertion
from app.core.internal_policy import InternalRoutePolicy
from app.ports.internal_caller import InternalCallerError

TOKEN = "x" * 40


def _keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


# ── shared-token driver (the non-breaking default) ────────────────────────────
def test_shared_token_accepts_the_right_token_as_an_unnamed_peer():
    c = SharedTokenCaller(TOKEN)
    ident = c.verify(token=TOKEN, assertion="", audience="svc")
    assert ident.caller == "" and ident.via == "shared"


def test_shared_token_rejects_wrong_or_empty():
    c = SharedTokenCaller(TOKEN)
    with pytest.raises(InternalCallerError):
        c.verify(token="nope", assertion="", audience="svc")
    with pytest.raises(InternalCallerError):
        c.verify(token="", assertion="", audience="svc")
    # an empty CONFIGURED token must never accept an empty presented token
    with pytest.raises(InternalCallerError):
        SharedTokenCaller("").verify(token="", assertion="", audience="svc")


# ── assertion driver (static keys) ─────────────────────────────────────────────
def test_a_valid_assertion_yields_the_named_caller():
    priv, pub = _keypair()
    caller = AssertionCaller(StaticKeyResolver({"billing": pub}), expected_token=TOKEN)
    a = mint_internal_assertion(private_key=priv, issuer="billing", audience="wallet")
    ident = caller.verify(token="", assertion=a, audience="wallet")
    assert ident.caller == "billing" and ident.via == "static"


def test_wrong_audience_is_denied():
    priv, pub = _keypair()
    caller = AssertionCaller(StaticKeyResolver({"billing": pub}), expected_token=TOKEN)
    a = mint_internal_assertion(private_key=priv, issuer="billing", audience="notification")
    with pytest.raises(InternalCallerError):
        caller.verify(token="", assertion=a, audience="wallet")   # minted for a different target


def test_expired_assertion_is_denied():
    priv, pub = _keypair()
    caller = AssertionCaller(StaticKeyResolver({"billing": pub}), expected_token=TOKEN)
    now = int(time.time())
    expired = jwt.encode({"iss": "billing", "aud": "wallet", "iat": now - 300, "exp": now - 60},
                         priv, algorithm="RS256")
    with pytest.raises(InternalCallerError):
        caller.verify(token="", assertion=expired, audience="wallet")


def test_unknown_caller_and_wrong_key_are_denied():
    priv, pub = _keypair()
    other_priv, _ = _keypair()
    caller = AssertionCaller(StaticKeyResolver({"billing": pub}), expected_token=TOKEN)
    # issuer not in the key map
    a_unknown = mint_internal_assertion(private_key=priv, issuer="stranger", audience="wallet")
    with pytest.raises(InternalCallerError):
        caller.verify(token="", assertion=a_unknown, audience="wallet")
    # right issuer, but signed with the WRONG key
    a_forged = mint_internal_assertion(private_key=other_priv, issuer="billing", audience="wallet")
    with pytest.raises(InternalCallerError):
        caller.verify(token="", assertion=a_forged, audience="wallet")


def test_token_fallback_during_migration():
    priv, pub = _keypair()
    caller = AssertionCaller(StaticKeyResolver({"billing": pub}),
                             allow_token_fallback=True, expected_token=TOKEN)
    # no assertion, correct flat token → accepted as unnamed peer
    assert caller.verify(token=TOKEN, assertion="", audience="wallet").caller == ""
    # fallback OFF → no assertion means denial even with the token
    strict = AssertionCaller(StaticKeyResolver({"billing": pub}),
                             allow_token_fallback=False, expected_token=TOKEN)
    with pytest.raises(InternalCallerError):
        strict.verify(token=TOKEN, assertion="", audience="wallet")


# ── per-route policy ────────────────────────────────────────────────────────────
def test_policy_defaults_open_and_enforces_named_lists():
    p = InternalRoutePolicy({
        "/internal/whatsapp/channel-usage": ["operations"],
        "/internal/backup": ["*"],
    })
    # no entry → any authenticated caller (default-open), including the unnamed peer
    assert p.permits("/internal/health", "") is True
    assert p.permits("/internal/health", "anyone") is True
    # named list → only that caller; the unnamed shared-token peer is refused
    assert p.permits("/internal/whatsapp/channel-usage", "operations") is True
    assert p.permits("/internal/whatsapp/channel-usage", "billing") is False
    assert p.permits("/internal/whatsapp/channel-usage", "") is False
    # "*" → any authenticated caller, named or unnamed
    assert p.permits("/internal/backup/export", "") is True      # longest-prefix under /internal/backup
    assert p.permits("/internal/backup/export", "anyone") is True


# ── caller side: internal_headers (mint when configured) round-trips to verify ────
class _Settings:
    def __init__(self, priv):
        self.INTERNAL_SERVICE_TOKEN = TOKEN
        self.SERVICE_NAME = "billing-service"
        self.INTERNAL_ASSERTION_PRIVATE_KEY = priv
        self.INTERNAL_ASSERTION_TTL_SECONDS = 60
        self.INTERNAL_ASSERTION_KID = ""


def test_internal_headers_round_trip_to_the_verifier():
    from app.core.internal_client import internal_headers
    priv, pub = _keypair()
    h = internal_headers(_Settings(priv), target="wallet-service")
    assert h["X-Internal-Token"] == TOKEN and "X-Internal-Assertion" in h
    caller = AssertionCaller(StaticKeyResolver({"billing-service": pub}), expected_token=TOKEN)
    ident = caller.verify(token=h["X-Internal-Token"], assertion=h["X-Internal-Assertion"],
                          audience="wallet-service")
    assert ident.caller == "billing-service" and ident.via == "static"


def test_internal_headers_are_token_only_when_no_signing_key():
    from app.core.internal_client import internal_headers
    s = _Settings("")   # no private key → signs nothing
    assert internal_headers(s, target="wallet-service") == {"X-Internal-Token": TOKEN}


def test_assertion_headers_are_add_only_and_verify():
    from app.core.internal_client import assertion_headers
    priv, pub = _keypair()
    h = assertion_headers(_Settings(priv), target="wallet-service")
    assert set(h) == {"X-Internal-Assertion"}          # ONLY the assertion — token left to the caller
    caller = AssertionCaller(StaticKeyResolver({"billing-service": pub}), expected_token=TOKEN)
    assert caller.verify(token="", assertion=h["X-Internal-Assertion"],
                         audience="wallet-service").caller == "billing-service"
    # no signing key → nothing added, so an unsigned service's headers are unchanged
    assert assertion_headers(_Settings(""), target="wallet-service") == {}
