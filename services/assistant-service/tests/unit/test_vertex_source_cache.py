"""The per-connection Vertex token source is REUSED across calls, and rebuilt only on rotation.

VertexTokenSource caches a short-lived OAuth token on the instance. Building a fresh one per model
call (the old behaviour) threw that cache away every time, so every question signed a JWT and made a
token-endpoint round trip on the hot path. These pin the reuse down.

The cache lives in core (connection_credentials.vertex_source) as a plain dict passed in — the
credential-resolution logic was moved out of the API layer so no route reads a secret's plaintext
(safety:no-secret-reads).
"""
from __future__ import annotations

from app.core.connection_credentials import vertex_source


def test_same_connection_and_secret_returns_the_same_instance():
    cache: dict = {}
    a = vertex_source(cache, "conn-1", "")
    b = vertex_source(cache, "conn-1", "")
    assert a is b                                   # cache hit — the token cache survives


def test_a_different_connection_gets_its_own_source():
    cache: dict = {}
    a = vertex_source(cache, "conn-1", "")
    b = vertex_source(cache, "conn-2", "")
    assert a is not b


def test_a_rotated_secret_rebuilds_the_source():
    # A valid SA JSON (construction checks the required keys are present, not their crypto validity).
    sa = '{"client_email":"a@x","private_key":"pk","token_uri":"https://t"}'
    cache: dict = {}
    a = vertex_source(cache, "conn-1", "")
    b = vertex_source(cache, "conn-1", sa)           # fingerprint mismatch -> rebuild
    assert a is not b
    # ...and the new one is then itself cached.
    assert vertex_source(cache, "conn-1", sa) is b
