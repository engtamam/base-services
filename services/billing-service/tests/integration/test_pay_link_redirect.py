"""Audit FIN-022 (+ FIN-014): the PUBLIC pay-link redirector route GET /api/v1/billing/pay/{token}.

The route's job is to 302 a token to its gateway checkout page AND enforce the anti-phishing / fail-safe
gates: https-only, host allow-list (fail-closed when unset), dead once the invoice is not open or is
missing, dead once expired, 404 for an unknown token. Only resolve_pay_link (core) was tested before;
these drive the ROUTE's status mapping and the open-redirect defense end-to-end.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.core.models import now_ts

SECRET = "h" * 40
ITOKEN = "i" * 40


def run(c):
    return asyncio.run(c)


def _make_app(allowed_hosts="pay.example.test"):
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": SECRET, "RATELIMIT_MAX": "1000"}
    if allowed_hosts is not None:
        env["BILLING_PAY_LINK_ALLOWED_HOSTS"] = allowed_hosts
    else:
        env.pop("BILLING_PAY_LINK_ALLOWED_HOSTS", None)
    os.environ.update(env)
    os.environ.pop("BILLING_PAY_LINK_ALLOWED_HOSTS", None) if allowed_hosts is None else None
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


def _seed(client, *, token, checkout_url, invoice_status="open", expires_in=10_000, with_invoice=True):
    ds = client.app.state.container.datastore
    if with_invoice:
        run(ds.put("invoices", "inv_r", {"id": "inv_r", "tenant": "t1", "number": "INV-R",
                                         "status": invoice_status, "total_minor": 5000, "currency": "SAR"}))
    run(ds.put("pay_links", token, {"id": token, "token": token, "tenant": "t1", "invoice_id": "inv_r",
                                    "payment_id": "pay_r", "checkout_url": checkout_url,
                                    "expires_at": now_ts() + expires_in, "status": "pending"}))


def test_happy_redirect_302_to_allowed_host(client):
    _seed(client, token="tok_ok", checkout_url="https://pay.example.test/session/5000")
    r = client.get("/api/v1/billing/pay/tok_ok", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://pay.example.test/session/5000"


def test_unknown_token_is_404(client):
    assert client.get("/api/v1/billing/pay/nope", follow_redirects=False).status_code == 404


def test_expired_link_is_410(client):
    _seed(client, token="tok_exp", checkout_url="https://pay.example.test/x", expires_in=-10)
    r = client.get("/api/v1/billing/pay/tok_exp", follow_redirects=False)
    assert r.status_code == 410


def test_settled_invoice_kills_the_link_410(client):
    _seed(client, token="tok_paid", checkout_url="https://pay.example.test/x", invoice_status="paid")
    r = client.get("/api/v1/billing/pay/tok_paid", follow_redirects=False)
    assert r.status_code == 410


def test_missing_invoice_is_dead_410_fin014(client):
    # FIN-014: the invoice row is gone -> the link must NOT 302 to a live gateway page.
    _seed(client, token="tok_missing", checkout_url="https://pay.example.test/x", with_invoice=False)
    r = client.get("/api/v1/billing/pay/tok_missing", follow_redirects=False)
    assert r.status_code == 410


def test_non_https_checkout_url_is_502(client):
    _seed(client, token="tok_http", checkout_url="http://pay.example.test/x")
    r = client.get("/api/v1/billing/pay/tok_http", follow_redirects=False)
    assert r.status_code == 502


def test_host_not_on_allowlist_is_502_open_redirect_block(client):
    _seed(client, token="tok_evil", checkout_url="https://evil.example/phish")
    r = client.get("/api/v1/billing/pay/tok_evil", follow_redirects=False)
    assert r.status_code == 502


def test_allowlist_unset_disables_the_redirector_503():
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        c = _make_app(allowed_hosts=None)          # BILLING_PAY_LINK_ALLOWED_HOSTS not configured
        _seed(c, token="tok_x", checkout_url="https://pay.example.test/x")
        r = c.get("/api/v1/billing/pay/tok_x", follow_redirects=False)
        assert r.status_code == 503                # fail-closed: redirector disabled until configured
    finally:
        os.environ.clear(); os.environ.update(saved)
        get_settings.cache_clear()
