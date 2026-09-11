"""Regression for authz-service-03: the body-size limit must hold for a CHUNKED body (no
Content-Length), validated through the real ASGI middleware stack.

httpx (TestClient) sends an iterator body as Transfer-Encoding: chunked with no Content-Length, so
these exercise exactly the branch the header check cannot see. The pre-fix middleware passed such a
body straight through uncapped; the fix counts streamed bytes and still replays the body downstream.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000", BODY_LIMIT_BYTES="2000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _internal():
    return {"X-Internal-Token": ITOKEN, "Content-Type": "application/json"}


def _chunked(payload: bytes):
    # An iterator body → httpx uses Transfer-Encoding: chunked, NO Content-Length.
    def gen():
        yield payload
    return gen()


def test_chunked_body_over_limit_is_rejected(client):
    big = b'{"x":"' + b"a" * 5000 + b'"}'   # ~5 KB, cap 2 KB, sent chunked
    r = client.post("/internal/authz/check", headers=_internal(), content=_chunked(big))
    assert r.status_code == 413


def test_chunked_body_under_limit_is_processed(client):
    # A valid, under-limit check sent chunked must reach the handler AND read its body (replayed).
    body = b'{"subject":"u1","action":"docs.read","resource":"doc1","tenant":"t1"}'
    r = client.post("/internal/authz/check", headers=_internal(), content=_chunked(body))
    assert r.status_code == 200
    assert "allow" in r.json()      # the handler saw the body and produced a decision
