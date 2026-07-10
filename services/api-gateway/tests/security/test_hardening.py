"""Security hardening properties — headers, tracing, deny-by-default, no leaks."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def secured_client() -> TestClient:
    from app.main import create_app

    app: FastAPI = create_app()

    @app.get("/boom")
    async def boom():  # noqa: D401 - deliberately raises to test error safety
        raise RuntimeError("super secret internal detail: db_password=hunter2")

    # The proxy catch-all is registered last, so a late-added /boom would be
    # shadowed. Move it to the front so the error-safety path is exercised.
    app.router.routes.insert(0, app.router.routes.pop())

    # raise_server_exceptions=False so our handler produces the response
    # instead of the test client re-raising.
    return TestClient(app, raise_server_exceptions=False)


def test_security_headers_present(secured_client):
    h = secured_client.get("/healthz").headers
    assert "content-security-policy" in h
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "referrer-policy" in h
    assert "permissions-policy" in h


def test_request_id_generated_and_echoed(secured_client):
    r = secured_client.get("/healthz")
    assert r.headers.get("x-request-id")


def test_request_id_accepts_valid_incoming(secured_client):
    rid = "abc-123_DEF"
    r = secured_client.get("/healthz", headers={"X-Request-Id": rid})
    assert r.headers["x-request-id"] == rid


def test_request_id_rejects_injection_attempt(secured_client):
    # A malicious id with CRLF / spaces must be replaced, not reflected (CWE-117).
    r = secured_client.get("/healthz", headers={"X-Request-Id": "bad id\r\nSet-Cookie: x"})
    assert r.headers["x-request-id"] != "bad id\r\nSet-Cookie: x"
    assert "\n" not in r.headers["x-request-id"]


def test_user_tier_deny_by_default(secured_client):
    # gateway has no own /api/v1 routes; an unconfigured path proxies to 404.
    assert secured_client.get("/api/v1/nope").status_code == 404


def test_unknown_path_generic_404(secured_client):
    r = secured_client.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"


def test_method_not_allowed_generic(secured_client):
    # On the gateway, POST to an own GET-only path is caught by the proxy -> 404
    # (no route), or 405 if matched by the tier. Either is a safe generic error.
    r = secured_client.post("/healthz")
    assert r.status_code in (404, 405)
    assert r.json()["error"] in ("NOT_FOUND", "METHOD_NOT_ALLOWED")


def test_unhandled_error_does_not_leak(secured_client):
    r = secured_client.get("/boom")
    assert r.status_code == 500
    body = r.text
    assert r.json()["error"] == "INTERNAL_ERROR"
    # The secret detail from the exception must never reach the client.
    assert "hunter2" not in body
    assert "db_password" not in body
    # Even error responses carry security headers.
    assert "content-security-policy" in r.headers
