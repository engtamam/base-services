"""Token verification must be OFFLOADED to a thread, not run inline on the event loop.

verify() is synchronous and, on the jwks/rs256 drivers, does blocking work (a JWKS HTTP fetch on a
kid/cache miss + CPU-bound RSA verify). Every service is single-worker uvicorn, so an inline call
blocks the ONE event loop for its whole duration — starving every other coroutine, including the one
reading the Redis GCRA reply for the same request's rate limit, which then times out and spuriously
degrades (observed as a 40%+ false-429 rate under load with genuinely distinct tenants).

This proves the fix structurally: verify() runs on a WORKER thread, not the loop thread. A future
revert to an inline `verifier.verify(token)` puts it back on the loop thread and fails here.
"""
from __future__ import annotations

import datetime as dt
import os
import threading

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40


@pytest.fixture()
def app():
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, PII_SALT="s" * 32,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET,
                      TOKEN_ISSUER="iss", TOKEN_AUDIENCE="aud", RATELIMIT_MAX="1000")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _bearer(caps=()):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "u1", "iss": "iss", "aud": "aud", "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def test_verify_runs_off_the_event_loop_thread(app):
    # Record the thread verify() executes on. The event loop runs on the MAIN thread under TestClient;
    # run_in_threadpool dispatches to a worker thread, so a correctly-offloaded verify is NOT on main.
    seen: dict[str, str] = {}
    verifier = app.app.state.container.token_verifier
    original = verifier.verify

    def _recording(token):
        seen["thread"] = threading.current_thread().name
        return original(token)

    verifier.verify = _recording

    # Any authenticated route runs require_user (which verifies BEFORE the capability check), so even a
    # 403 proves verify ran. Hit an operator route with a valid-signature token.
    app.get("/api/v1/operator/secrets", headers=_bearer(caps=("platform.operator",)))

    assert "thread" in seen, "verify() was never called on the request path"
    assert seen["thread"] != threading.main_thread().name, (
        f"verify() ran on {seen['thread']!r}, the event-loop thread — it must be offloaded to a "
        f"worker thread (run_in_threadpool), or it blocks the single-worker loop")
    assert seen["thread"] != threading.current_thread().name
