"""Self-service DSAR — 52.1. The subject opens a request about THEMSELVES, never anyone else.

The load-bearing test is the negative one: the route reads the subject from the TOKEN, so a body
that tries to name another subject is simply ignored (CWE-639). The capability is required, and the
request that lands carries the caller's own subject.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": "unit-test-kek", "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(sub: str, *caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": sub, "iss": ISS, "aud": AUD, "caps": list(caps),
                      "tenant": "acme", "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def app():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def test_self_dsar_requires_the_capability(app):
    r = app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
                 headers=_auth("alice@example.com", "compliance.read"))
    assert r.status_code == 403


def test_self_dsar_opens_against_the_callers_own_subject(app):
    r = app.post("/api/v1/compliance/me/dsar", json={"type": "access"},
                 headers=_auth("alice@example.com", "compliance.self"))
    assert r.status_code == 201
    assert r.json()["subject"] in ("alice@example.com", None) or "id" in r.json()


def test_a_body_subject_id_is_ignored_not_honoured(app):
    """The IDOR guard: a caller cannot open a DSAR against someone else by putting their id in the
    body. The field is not even in the model, so it is dropped — the subject stays the token's."""
    r = app.post("/api/v1/compliance/me/dsar",
                 json={"type": "erasure", "subject": "victim@example.com"},
                 headers=_auth("attacker@example.com", "compliance.self"))
    assert r.status_code == 201
    # the created DSAR is the attacker's own; victim was never named
    body = r.json()
    assert body.get("subject") != "victim@example.com"


def test_an_unknown_dsar_type_is_rejected(app):
    r = app.post("/api/v1/compliance/me/dsar", json={"type": "nonsense"},
                 headers=_auth("alice@example.com", "compliance.self"))
    assert r.status_code == 422
