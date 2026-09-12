"""Shared test setup.

Sets a valid environment BEFORE the app is imported so config validation passes,
then exposes a TestClient. ENV=test keeps logging silent and disables prod-only
behavior (HSTS, hidden docs).
"""
from __future__ import annotations

import os

# Must be set before any `app.*` import triggers config validation.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "t" * 40)
os.environ.setdefault("BODY_LIMIT_BYTES", "2048")
os.environ.setdefault("REQUEST_TIMEOUT_MS", "30000")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


INTERNAL_TOKEN = os.environ["INTERNAL_SERVICE_TOKEN"]


@pytest.fixture(autouse=True)
def _reset_refresh_locks():
    """Each unit test runs its own asyncio.run() (a fresh event loop). TokenManager keeps a
    process-global dict of per-connection asyncio.Locks created lazily in whatever loop first refreshes;
    a lock created in one test's loop is bound to it, so reusing the same connection id in the next
    test's loop raises 'bound to a different event loop'. Clear it between tests so each starts clean.
    Production has one long-lived loop, so this only affects the test harness."""
    try:
        from app.core import token_manager as _tm
        _tm._LOCKS.clear()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session")
def client() -> TestClient:
    from app.main import app

    return TestClient(app)
