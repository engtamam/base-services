"""Off-gateway settlement over HTTP.

The security shape: a duplicate must return a generic 409, never revealing which tenant owns
the original transfer. And a tenant may only settle its own invoices.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40
NOW = time.time()


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      RATELIMIT_MAX="100000",
                      SETTLEMENT_METHODS="bank_transfer:issuer,cash:no-issuer",
                      SETTLEMENT_TOLERANCE_MINOR="50")
    get_settings.cache_clear()
    try:
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


ADMIN = ["platform.billing"]
SELF = ["billing.self"]


def make_invoice(client, tenant="t1", total=100_000):
    """An open invoice, issued 30 days ago — an enterprise pays after the invoice, not before."""
    import asyncio

    from app.core.models import Invoice, new_id
    from app.core.stores import InvoiceStore

    store = InvoiceStore(client.app.state.container.datastore)
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    inv = Invoice(id=new_id("inv_"), tenant=tenant, number="INV-X", total_minor=total,
                  currency="SAR", status="open", created=created)
    asyncio.run(store.save(inv))
    return inv


def settle(client, invoice_id, headers, **body):
    body.setdefault("method", "bank_transfer")
    body.setdefault("issuer", "SABB")
    body.setdefault("reference", "MT103-1")
    body.setdefault("amount_minor", 100_000)
    body.setdefault("paid_at", NOW)
    return client.post(f"/api/v1/billing/invoices/{invoice_id}/settle", json=body, headers=headers)


# ── auth ────────────────────────────────────────────────────────────────────
def test_settling_requires_a_token(client):
    inv = make_invoice(client)
    assert client.post(f"/api/v1/billing/invoices/{inv.id}/settle", json={}).status_code == 401


def test_settling_requires_the_manage_capability(client):
    inv = make_invoice(client)
    r = settle(client, inv.id, tok(SELF))            # billing.self may read, not settle
    assert r.status_code == 403


# ── the happy path ──────────────────────────────────────────────────────────
def test_a_bank_transfer_settles_the_invoice(client):
    inv = make_invoice(client)
    r = settle(client, inv.id, tok(ADMIN), reference="WIRE-1", actor="finance@acme.com")
    assert r.status_code == 200
    body = r.json()
    assert body["invoice_status"] == "paid"
    assert body["remaining_minor"] == 0
    assert body["einvoice_finalized"] is True
    assert body["settlement"]["actor"] == "finance@acme.com"


def test_a_partial_settlement_reports_what_is_left(client):
    inv = make_invoice(client)
    r = settle(client, inv.id, tok(ADMIN), reference="P1", amount_minor=40_000).json()
    assert r["invoice_status"] == "partially_paid"
    assert r["remaining_minor"] == 60_000
    assert r["einvoice_finalized"] is False


def test_settlements_are_listed(client):
    inv = make_invoice(client)
    settle(client, inv.id, tok(ADMIN), reference="L1", amount_minor=40_000)
    settle(client, inv.id, tok(ADMIN), reference="L2", amount_minor=60_000)
    rows = client.get(f"/api/v1/billing/invoices/{inv.id}/settlements", headers=tok(ADMIN)).json()
    assert rows["count"] == 2


# ── the identity of a payment ───────────────────────────────────────────────
def test_the_same_reference_from_two_banks_both_settle(client):
    a, b = make_invoice(client), make_invoice(client)
    assert settle(client, a.id, tok(ADMIN), issuer="SABB", reference="SHARED").status_code == 200
    assert settle(client, b.id, tok(ADMIN), issuer="ALRAJHI", reference="SHARED").status_code == 200


def test_one_transfer_cannot_settle_two_invoices_and_the_error_is_generic(client):
    a = make_invoice(client, tenant="victim")
    b = make_invoice(client, tenant="attacker")
    assert settle(client, a.id, tok(ADMIN), reference="ONCE").status_code == 200
    r = settle(client, b.id, tok(ADMIN), reference="ONCE")
    assert r.status_code == 409
    text = r.text
    assert "victim" not in text and a.id not in text     # no oracle


def test_cash_needs_no_issuer(client):
    inv = make_invoice(client)
    r = settle(client, inv.id, tok(ADMIN), method="cash", issuer="", reference="RCPT-9")
    assert r.status_code == 200 and r.json()["settlement"]["issuer"] == "t1"


def test_an_unknown_method_is_a_400(client):
    inv = make_invoice(client)
    assert settle(client, inv.id, tok(ADMIN), method="crypto").status_code == 400


def test_a_missing_issuer_is_a_400(client):
    inv = make_invoice(client)
    assert settle(client, inv.id, tok(ADMIN), issuer="", reference="NOISS").status_code == 400


# ── tenant isolation ────────────────────────────────────────────────────────
def test_a_tenant_cannot_settle_another_tenants_invoice(client):
    inv = make_invoice(client, tenant="victim2")
    r = settle(client, inv.id, tok(["billing.manage"], tenant="attacker"), reference="X1")
    assert r.status_code == 404                          # never confirm existence


# ── value date + amounts ────────────────────────────────────────────────────
def test_a_future_value_date_is_a_400(client):
    inv = make_invoice(client)
    assert settle(client, inv.id, tok(ADMIN), paid_at=NOW + 86400).status_code == 400


def test_overpayment_is_a_400(client):
    inv = make_invoice(client)
    assert settle(client, inv.id, tok(ADMIN), amount_minor=500_000).status_code == 400


def test_a_bank_fee_within_tolerance_closes_the_invoice(client):
    inv = make_invoice(client)
    r = settle(client, inv.id, tok(ADMIN), reference="FEE-1", amount_minor=99_980)
    assert r.json()["invoice_status"] == "paid"


# ── reversal ────────────────────────────────────────────────────────────────
def test_a_recalled_transfer_reverses_and_issues_a_credit_note(client):
    inv = make_invoice(client)
    sid = settle(client, inv.id, tok(ADMIN), reference="RCL-1").json()["settlement"]["id"]
    r = client.post(f"/api/v1/billing/settlements/{sid}/reverse",
                    json={"reason": "recalled by bank", "actor": "fin"}, headers=tok(ADMIN))
    assert r.status_code == 200
    assert r.json()["credit_note"]["amount_minor"] == 100_000
    assert r.json()["invoice_status"] == "open"


def test_reversing_requires_manage(client):
    inv = make_invoice(client)
    sid = settle(client, inv.id, tok(ADMIN), reference="RCL-2").json()["settlement"]["id"]
    r = client.post(f"/api/v1/billing/settlements/{sid}/reverse", json={}, headers=tok(SELF))
    assert r.status_code == 403


def test_reversing_an_unknown_settlement_is_a_404(client):
    r = client.post("/api/v1/billing/settlements/stl_nope/reverse", json={}, headers=tok(ADMIN))
    assert r.status_code == 404


def test_manifest_advertises_settlement(client):
    m = client.get("/internal/manifest", headers={"X-Internal-Token": INTERNAL}).json()
    paths = {e["path"] for e in m["endpoints"]}
    assert "/api/v1/billing/invoices/{invoice_id}/settle" in paths
