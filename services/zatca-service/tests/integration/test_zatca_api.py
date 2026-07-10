"""Integration: internal finalize/sweep + admin settings/invoices/chain + gating."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40
INV = {"number": "INV-1", "issued_at": 1_700_000_000, "seller": {"name": "ACME", "vat_number": "3000"},
       "lines": [{"description": "Plan", "qty": 1, "amount_minor": 10_000}]}


def _make_app() -> TestClient:
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


def _adm(caps=("platform.zatca",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "a", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _int():
    return {"X-Internal-Token": ITOKEN}


def test_settings_view_has_no_csid(client):
    r = client.get("/api/v1/zatca/settings", headers=_adm())
    assert r.status_code == 200
    body = r.json()
    assert body["onboarded"] is False
    assert "csid_cert_enc" not in body and "private_key_enc" not in body

    client.put("/api/v1/zatca/settings", json={"seller_name": "ACME", "environment": "simulation"},
               headers=_adm())
    assert client.get("/api/v1/zatca/settings", headers=_adm()).json()["seller_name"] == "ACME"


def test_finalize_fail_closed_then_chain(client):
    r = client.post("/internal/zatca/finalize", json={"tenant": "t1", "invoice": INV}, headers=_int())
    assert r.status_code == 200 and r.json()["status"] == "pending"       # null signer, fail-closed
    chain = client.get("/api/v1/zatca/chain/verify", headers=_adm())
    assert chain.json()["ok"] is True
    na = client.get("/api/v1/zatca/invoices?needs_attention=true", headers=_adm()).json()
    assert len(na["invoices"]) >= 1 and "signed_xml" not in na["invoices"][0]  # bodies omitted


def test_finalize_requires_number(client):
    r = client.post("/internal/zatca/finalize", json={"tenant": "t1", "invoice": {"lines": []}}, headers=_int())
    assert r.status_code == 422


def test_admin_needs_capability(client):
    assert client.get("/api/v1/zatca/settings", headers=_adm(caps=())).status_code == 403


def test_internal_endpoints_need_token(client):
    assert client.post("/internal/zatca/finalize", json={"tenant": "t1", "invoice": INV}).status_code == 401
    assert client.post("/internal/zatca/sweep").status_code == 401


def test_tenant_admin_scoped(client):
    own = _adm(caps=("zatca.manage",), tenant="tenant-A")
    client.put("/api/v1/zatca/settings", json={"seller_name": "A-Corp", "tenant": "tenant-B"}, headers=own)
    # forced to own tenant — reading back tenant-A shows it, tenant-B untouched
    assert client.get("/api/v1/zatca/settings", headers=own).json()["seller_name"] == "A-Corp"


def test_qr_unavailable_with_null_renderer(client):
    fid = client.get("/api/v1/zatca/invoices", headers=_adm()).json()["invoices"][0]["id"]
    assert client.get(f"/api/v1/zatca/invoices/{fid}/qr", headers=_adm()).status_code == 404


def test_qr_image_and_data_uri_with_segno():
    import asyncio

    pytest = __import__("pytest")
    pytest.importorskip("segno")
    from app.config import get_settings

    saved = dict(os.environ)
    try:
        os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                          TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000",
                          QR_RENDERER_DRIVER="segno", QR_FORMAT="png")
        get_settings.cache_clear()
        from app.main import create_app
        from app.core.models import ZatcaInvoice

        app = TestClient(create_app(), raise_server_exceptions=False)
        rec = ZatcaInvoice(id="qr1", tenant="t1", number="Q-1", icv=1, status="reported", qr="AQVBQ01F")
        asyncio.new_event_loop().run_until_complete(
            app.app.state.container.datastore.put("zatca_invoices", "qr1", rec.to_dict()))
        img = app.get("/api/v1/zatca/invoices/qr1/qr", headers=_adm())
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        du = app.get("/api/v1/zatca/invoices/qr1/qr?data_uri=true", headers=_adm()).json()
        assert du["data_uri"].startswith("data:image/png;base64,")
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()
