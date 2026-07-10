"""Security: no raw card, secret-free, webhook signature, deny-by-default."""
from __future__ import annotations

import datetime as dt
import inspect
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app():
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_deny_by_default(client):
    assert client.get("/api/v1/billing/invoices").status_code == 401
    assert client.post("/api/v1/billing/plans", json={}).status_code == 401


def test_no_raw_card_in_charge_signature():
    # the gateway contract takes a TOKEN, never raw PAN/cvv (PCI SAQ-A)
    from app.ports.payment_gateway import PaymentGateway
    params = set(inspect.signature(PaymentGateway.charge).parameters)
    assert "token" in params
    assert not ({"pan", "card_number", "cvv", "cvc"} & params)


def test_webhook_rejects_bad_signature(client):
    # default null gateway -> verify_webhook returns verified False -> 400
    r = client.post("/internal/billing/webhook/null", content=b"{}",
                    headers={"X-Internal-Token": ITOKEN})
    assert r.status_code == 400


def test_null_gateway_never_fabricates_a_charge():
    from app.adapters.payment.null import NullGateway
    from app.ports.payment_gateway import GatewayUnavailable
    with pytest.raises(GatewayUnavailable):
        NullGateway().charge(amount_minor=100, currency="SAR", token="x", idempotency_key="k")


def test_secret_free_gateway_errors():
    # adapter unreachable errors carry no key material
    from app.adapters.payment.stripe import StripeGateway
    import app.adapters.payment.stripe as S
    S.post_form = lambda u, f, h=None: (0, {})
    from app.ports.payment_gateway import GatewayUnavailable
    try:
        StripeGateway("sk_secret_value").charge(amount_minor=100, currency="SAR", token="pm", idempotency_key="k")
        assert False
    except GatewayUnavailable as e:
        assert "sk_secret_value" not in str(e)
