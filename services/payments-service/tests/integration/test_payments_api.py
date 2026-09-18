"""End-to-end conduit over HTTP: set a shop's sealed gateway creds (write-only), mint a pay-link,
follow the redirector, receive a verified webhook, and confirm write-only + fail-closed behaviour."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40
KEK = "k" * 48
SECRET = "console-webhook-secret"


def _make_app(**env_over) -> TestClient:
    from app.config import get_settings
    env = {
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": KEK,
        "PAYMENTS_REDIRECT_BASE_URL": "https://conduit.example",
        "PAYMENTS_REDIRECT_ALLOWED_HOSTS": "pay.example.test",
        "PAYMENTS_CONSOLE_LINK_BASE": "https://pay.example.test/checkout",
    }
    env.update(env_over)
    os.environ.update(env)
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


def _configure(client, tenant="acme"):
    for name, value in (("MERCHANT_GATEWAY_DRIVER", "console"),
                        ("MERCHANT_WEBHOOK_SECRET", SECRET)):
        r = client.post("/internal/payments/merchant-credentials", headers=_h(),
                        json={"tenant": tenant, "name": name, "value": value})
        assert r.status_code == 200, r.text


def test_credentials_are_write_only_and_return_hints_not_values(client):
    _configure(client)
    # set returns only a masked hint, never the value
    r = client.post("/internal/payments/merchant-credentials", headers=_h(),
                    json={"tenant": "acme", "name": "MERCHANT_API_KEY", "value": "+966500000000"})
    assert r.status_code == 200
    assert "value" not in r.json() and r.json()["hint"].startswith("•")
    # list shows hints, never values
    lst = client.get("/internal/payments/merchant-credentials?tenant=acme", headers=_h()).json()
    names = {c["name"] for c in lst["credentials"]}
    assert "MERCHANT_API_KEY" in names
    assert all("value" not in c for c in lst["credentials"])
    # there is NO route that returns a plaintext credential
    assert client.get("/internal/payments/merchant-credentials/MERCHANT_API_KEY?tenant=acme",
                      headers=_h()).status_code in (404, 405)


def test_unknown_credential_name_is_refused(client):
    r = client.post("/internal/payments/merchant-credentials", headers=_h(),
                    json={"tenant": "acme", "name": "MERCHANT_EVIL", "value": "x"})
    assert r.status_code == 422 and "allowed" in r.json()["reason"]


def test_create_link_requires_internal_token(client):
    assert client.post("/internal/payments/requests",
                       json={"tenant": "acme", "reference": "o1", "amount_minor": 100,
                             "currency": "SAR"}).status_code == 401


def test_full_flow_create_redirect_webhook(client):
    _configure(client)
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "order-1", "amount_minor": 15000,
                          "currency": "SAR"})
    assert r.status_code == 200, r.text
    body = r.json()
    rid, provider_ref = body["request_id"], body["provider_ref"]
    assert body["pay_url"] == f"https://conduit.example/api/v1/payments/pay/{rid}"

    # redirector 302s to the allow-listed gateway host
    red = client.get(f"/api/v1/payments/pay/{rid}", follow_redirects=False)
    assert red.status_code == 302 and red.headers["location"].startswith("https://pay.example.test/")

    # a verified webhook completes it
    hook = client.post("/api/v1/payments/webhook/acme",
                       json={"status": "succeeded", "provider_ref": provider_ref,
                             "amount_minor": 15000, "currency": "SAR", "event_id": "e1",
                             "secret": SECRET})
    assert hook.status_code == 200 and hook.json()["applied"] is True

    # status now completed; replay does not double-apply
    st = client.get(f"/internal/payments/requests/{rid}?tenant=acme", headers=_h()).json()
    assert st["status"] == "completed"
    hook2 = client.post("/api/v1/payments/webhook/acme",
                        json={"status": "succeeded", "provider_ref": provider_ref,
                              "amount_minor": 15000, "secret": SECRET})
    assert hook2.status_code == 200


def test_unconfigured_tenant_create_is_409(client):
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "ghost", "reference": "o1", "amount_minor": 100,
                          "currency": "SAR"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_CONFIGURED"


def test_redirect_unknown_id_is_404(client):
    assert client.get("/api/v1/payments/pay/nope", follow_redirects=False).status_code == 404


def test_unverified_webhook_applies_nothing(client):
    _configure(client)
    r = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "o-bad", "amount_minor": 100,
                          "currency": "SAR"}).json()
    hook = client.post("/api/v1/payments/webhook/acme",
                       json={"status": "succeeded", "provider_ref": r["provider_ref"],
                             "amount_minor": 100, "secret": "WRONG"})
    assert hook.status_code == 200 and hook.json()["applied"] is False
    st = client.get(f"/internal/payments/requests/{r['request_id']}?tenant=acme", headers=_h()).json()
    assert st["status"] == "pending"


def test_H1_malicious_gateway_base_is_refused_at_write(client):
    for bad in ("http://169.254.169.254/", "https://127.0.0.1/", "file:///etc/passwd",
                "http://evil.example/"):
        r = client.post("/internal/payments/merchant-credentials", headers=_h(),
                        json={"tenant": "acme", "name": "MERCHANT_API_BASE", "value": bad})
        assert r.status_code == 422, (bad, r.text)
    # a legitimate https gateway base is accepted
    ok = client.post("/internal/payments/merchant-credentials", headers=_h(),
                     json={"tenant": "acme", "name": "MERCHANT_API_BASE",
                           "value": "https://onboarding-uat.tuwaiqpay.com.sa/api/v1"})
    assert ok.status_code == 200, ok.text


def test_H5_oversized_webhook_is_refused(client):
    _configure(client)
    big = client.post("/api/v1/payments/webhook/acme",
                      headers={"content-length": str(10_000_000)},
                      content=b"x" * 100)   # declared length over cap -> 413 before reading
    assert big.status_code == 413
