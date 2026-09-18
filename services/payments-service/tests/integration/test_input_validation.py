"""Create-request input validation at the internal boundary: unsupported currency (M3), over-ceiling
amount (L7) and oversized metadata (L8) are each a clear 422, never a silent charge."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40
KEK = "k" * 48


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": KEK,
        "PAYMENTS_REDIRECT_BASE_URL": "https://conduit.example",
        "PAYMENTS_REDIRECT_ALLOWED_HOSTS": "pay.example.test",
        "PAYMENTS_CONSOLE_LINK_BASE": "https://pay.example.test/checkout",
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


def test_unsupported_currency_is_422(client):
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o1", "amount_minor": 100,
                          "currency": "ZZZ"})
    assert r.status_code == 422 and "currency" in r.json()["reason"]


def test_over_ceiling_amount_is_422(client):
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o2", "amount_minor": 1_000_000_001,
                          "currency": "SAR"})
    assert r.status_code == 422


def test_oversized_metadata_is_422(client):
    meta = {f"k{i}": "x" for i in range(40)}          # > _MAX_METADATA_KEYS
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o3", "amount_minor": 100,
                          "currency": "SAR", "metadata": meta})
    assert r.status_code == 422 and "metadata" in r.json()["reason"]
