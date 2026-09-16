"""POST/GET /internal/whatsapp/catalog* — the catalog lifecycle over the internal tier.

Security surface: internal-token only. Behaviour under the default console manager (no real WABA):
create returns a synthetic id, connect + commerce-settings succeed, list is empty.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40
IT = {"X-Internal-Token": ITOKEN}


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="100000")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app(), raise_server_exceptions=False) as c:
            yield c
    finally:
        os.environ.clear(); os.environ.update(saved)
        get_settings.cache_clear()


def test_catalog_routes_require_the_internal_token(client):
    assert client.post("/internal/whatsapp/catalog",
                       json={"tenant": "t1", "name": "Shop"}).status_code == 401
    assert client.get("/internal/whatsapp/catalogs?tenant=t1").status_code == 401
    assert client.post("/internal/whatsapp/commerce-settings",
                       json={"tenant": "t1", "phone_number_id": "p"}).status_code == 401


def test_create_then_connect_a_catalog(client):
    r = client.post("/internal/whatsapp/catalog", headers=IT, json={"tenant": "acme", "name": "Acme Shop"})
    assert r.status_code == 201, r.text
    cid = r.json()["catalog_id"]
    assert cid and r.json()["ok"] is True

    c = client.post(f"/internal/whatsapp/catalog/{cid}/connect", headers=IT, json={"tenant": "acme"})
    assert c.status_code == 200 and c.json()["ok"] is True

    g = client.get(f"/internal/whatsapp/catalog/{cid}?tenant=acme", headers=IT)
    assert g.status_code == 200 and g.json()["ok"] is True


def test_commerce_settings_succeeds(client):
    r = client.post("/internal/whatsapp/commerce-settings", headers=IT,
                    json={"tenant": "acme", "phone_number_id": "PN1",
                          "is_cart_enabled": True, "is_catalog_visible": True})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_list_catalogs_is_empty_on_console(client):
    r = client.get("/internal/whatsapp/catalogs?tenant=acme", headers=IT)
    assert r.status_code == 200 and r.json() == {"catalogs": []}


def test_sync_products_batch(client):
    r = client.post("/internal/whatsapp/catalog/cat_1/products", headers=IT, json={
        "tenant": "acme",
        "upserts": [{"retailer_id": "p1", "name": "Cut", "price_minor": 12900, "currency": "SAR"}],
        "deletes": ["old1"]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["count"] == 2


def test_sync_products_requires_the_token(client):
    assert client.post("/internal/whatsapp/catalog/cat_1/products",
                       json={"tenant": "acme", "upserts": []}).status_code == 401


def test_sync_products_over_the_cap_is_refused(client):
    big = [{"retailer_id": f"p{i}", "name": "x", "price_minor": 1} for i in range(3001)]
    r = client.post("/internal/whatsapp/catalog/cat_1/products", headers=IT,
                    json={"tenant": "acme", "upserts": big})
    assert r.status_code == 400 and "exceeds" in r.json().get("error", "").lower() or r.status_code == 400
