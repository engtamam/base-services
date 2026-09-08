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
# Audit records now pseudonymise a subject in resource= via subject_ref, which REFUSES an empty salt
# (as production does). Tests exercise those audited paths, so they need a salt like production has.
os.environ.setdefault("PII_SALT", "s" * 32)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


INTERNAL_TOKEN = os.environ["INTERNAL_SERVICE_TOKEN"]


@pytest.fixture(scope="session")
def client() -> TestClient:
    from app.main import app

    return TestClient(app)
