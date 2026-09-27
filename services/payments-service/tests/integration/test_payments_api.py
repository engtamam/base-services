"""End-to-end conduit over HTTP: set a shop's sealed gateway creds (write-only), mint a pay-link,
follow the redirector, receive a verified webhook, and confirm write-only + fail-closed behaviour."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient


def run(c):
    return asyncio.run(c)

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


def test_recreate_same_reference_different_amount_is_409(client):
    # FIN-017: an edited order (same reference, new amount) must NOT silently return the stale link.
    _configure(client)
    a = client.post("/internal/payments/requests", headers=_h(),
                    json={"tenant": "acme", "reference": "ord-9", "amount_minor": 10000,
                          "currency": "SAR"})
    assert a.status_code == 200
    # identical amount reuses the same request (idempotent)
    same = client.post("/internal/payments/requests", headers=_h(),
                       json={"tenant": "acme", "reference": "ord-9", "amount_minor": 10000,
                             "currency": "SAR"})
    assert same.status_code == 200 and same.json()["request_id"] == a.json()["request_id"]
    # a DIFFERENT amount for the same reference is refused, not billed at the stale amount
    diff = client.post("/internal/payments/requests", headers=_h(),
                       json={"tenant": "acme", "reference": "ord-9", "amount_minor": 15000,
                             "currency": "SAR"})
    assert diff.status_code == 409 and diff.json()["code"] == "REQUEST_MISMATCH"


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


# ── FIN-023 / FIN-008: the reconcile HTTP route (recover a missed webhook, at the route layer) ───────
def _open_request(client, tenant="acme", ref="rec-1", amount=15000):
    _configure(client, tenant)
    return client.post("/internal/payments/requests", headers=_h(),
                       json={"tenant": tenant, "reference": ref, "amount_minor": amount,
                             "currency": "SAR"}).json()


def test_reconcile_requires_internal_token(client):
    r = _open_request(client)
    assert client.post(f"/internal/payments/requests/{r['request_id']}/reconcile?tenant=acme"
                       ).status_code == 401


def test_reconcile_unknown_id_is_404(client):
    _configure(client)
    assert client.post("/internal/payments/requests/nope/reconcile?tenant=acme",
                       headers=_h()).status_code == 404


def test_reconcile_wrong_tenant_is_404_idor_guard(client):
    # A tenant must not reconcile another tenant's request id (the cross-tenant guard, FIN-008).
    r = _open_request(client, tenant="acme", ref="rec-idor")
    _configure(client, "intruder")
    got = client.post(f"/internal/payments/requests/{r['request_id']}/reconcile?tenant=intruder",
                      headers=_h())
    assert got.status_code == 404


def test_reconcile_recovers_a_missed_webhook_to_completed(client, monkeypatch):
    # The webhook never arrived; a manual reconcile pulls the gateway status and completes the invoice
    # through the SAME idempotent seam. Drive it by having the (console) gateway report SUCCEEDED.
    import app.adapters.payment.console as console
    from app.ports.payment_gateway import SUCCEEDED, PaymentOutcome
    r = _open_request(client, ref="rec-ok", amount=15000)

    def _succeeded(self, *, provider_ref, currency=""):
        return PaymentOutcome(status=SUCCEEDED, provider="console", provider_ref=provider_ref,
                              amount_minor=15000, currency="SAR", verified=True)

    monkeypatch.setattr(console.ConsoleGateway, "fetch_status", _succeeded)
    got = client.post(f"/internal/payments/requests/{r['request_id']}/reconcile?tenant=acme",
                      headers=_h())
    assert got.status_code == 200 and got.json()["status"] == "completed"
    # idempotent: a second reconcile does not double-apply, still completed
    again = client.post(f"/internal/payments/requests/{r['request_id']}/reconcile?tenant=acme",
                        headers=_h())
    assert again.status_code == 200 and again.json()["status"] == "completed"


def test_reconcile_gateway_unavailable_is_502(client, monkeypatch):
    import app.adapters.payment.console as console
    from app.ports.payment_gateway import GatewayUnavailable
    r = _open_request(client, ref="rec-down")

    def _down(self, *, provider_ref, currency=""):
        raise GatewayUnavailable("down")

    monkeypatch.setattr(console.ConsoleGateway, "fetch_status", _down)
    got = client.post(f"/internal/payments/requests/{r['request_id']}/reconcile?tenant=acme",
                      headers=_h())
    assert got.status_code == 502 and got.json()["code"] == "GATEWAY_UNAVAILABLE"


# ── FIN-010 / FIN-013: the redirector refuses a terminal/expired/unknown link IDENTICALLY (no oracle) ─
def test_redirector_refuses_a_completed_link(client):
    r = _open_request(client, ref="red-done")
    prov = client.get(f"/internal/payments/requests/{r['request_id']}?tenant=acme",
                      headers=_h()).json()["provider_ref"]
    client.post("/api/v1/payments/webhook/acme",
                json={"status": "succeeded", "provider_ref": prov, "amount_minor": 15000,
                      "currency": "SAR", "secret": SECRET})
    dead = client.get(f"/api/v1/payments/pay/{r['request_id']}", follow_redirects=False)
    assert dead.status_code == 404                          # a paid link is dead, not a 302 to the gateway


def test_redirector_refuses_an_expired_link(client):
    r = _open_request(client, ref="red-exp")
    ds = client.app.state.container.datastore
    row = run(ds.get("payment_requests", r["request_id"]))
    row["expires_at"] = 1                                   # force past-TTL while still pending
    run(ds.put("payment_requests", r["request_id"], row))
    dead = client.get(f"/api/v1/payments/pay/{r['request_id']}", follow_redirects=False)
    assert dead.status_code == 404


def test_redirector_no_existence_oracle_unknown_vs_spent_vs_expired_are_identical(client):
    # FIN-013: unknown, completed, and expired must be INDISTINGUISHABLE (same status AND same body),
    # so a link holder cannot learn whether an id exists. Only a live link differs (302).
    unknown = client.get("/api/v1/payments/pay/does-not-exist", follow_redirects=False)

    done = _open_request(client, ref="oracle-done")
    prov = client.get(f"/internal/payments/requests/{done['request_id']}?tenant=acme",
                      headers=_h()).json()["provider_ref"]
    client.post("/api/v1/payments/webhook/acme",
                json={"status": "succeeded", "provider_ref": prov, "amount_minor": 15000,
                      "currency": "SAR", "secret": SECRET})
    spent = client.get(f"/api/v1/payments/pay/{done['request_id']}", follow_redirects=False)

    exp = _open_request(client, ref="oracle-exp")
    ds = client.app.state.container.datastore
    row = run(ds.get("payment_requests", exp["request_id"])); row["expires_at"] = 1
    run(ds.put("payment_requests", exp["request_id"], row))
    expired = client.get(f"/api/v1/payments/pay/{exp['request_id']}", follow_redirects=False)

    codes = {unknown.status_code, spent.status_code, expired.status_code}
    bodies = {unknown.json()["reason"], spent.json()["reason"], expired.json()["reason"]}
    assert codes == {404} and len(bodies) == 1              # one status, one body — no leak


# ── FIN-026: an oversized webhook body is refused (413) ──────────────────────────────────────────────
def test_oversized_webhook_body_is_refused_413():
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        c = _make_app(PAYMENTS_MAX_WEBHOOK_BYTES="20")     # tiny cap
        _configure(c)
        big = c.post("/api/v1/payments/webhook/acme", content=b"x" * 500)
        assert big.status_code == 413                       # refused before processing
    finally:
        os.environ.clear(); os.environ.update(saved)
        get_settings.cache_clear()
