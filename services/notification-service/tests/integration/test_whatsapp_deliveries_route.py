"""The operator diagnostic route GET /internal/whatsapp/deliveries: internal-token guarded,
addressable by tenant OR by the shop's phone_number_id (resolved through the number index), tenant-
scoped, and it masks the recipient. This is the API the 'why is this shop's WhatsApp broken' screen calls."""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.models import Delivery
from app.core.stores import DeliveryStore, WhatsAppNumberIndex
from app.main import create_app


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def client():
    get_settings.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def _seed(app, *, tenant, recipient, status, error="", phone_number_id=""):
    ds = app.state.container.datastore
    run(DeliveryStore(ds).record(Delivery(
        id="d-" + status + recipient[-2:], recipient=recipient, channel="whatsapp",
        template_key="otp", status=status, tenant=tenant, error=error,
        provider="meta", provider_message_id="wamid.x", receipt_status="delivered")))
    if phone_number_id:
        run(WhatsAppNumberIndex(ds).bind(phone_number_id, tenant))


def test_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/deliveries?tenant=acme").status_code == 401


def test_lists_a_tenants_whatsapp_deliveries_with_masked_recipient(client):
    _seed(client.app, tenant="acme", recipient="+966500001234", status="failed",
          error="template not approved")
    r = client.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok())
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "acme" and len(body["deliveries"]) == 1
    row = body["deliveries"][0]
    assert row["status"] == "failed" and row["error"] == "template not approved"
    assert row["receipt_status"] == "delivered"
    # the recipient is masked to its last four — never the full number
    assert row["recipient_hint"] == "•••1234"
    assert "+966500001234" not in r.text


def test_resolves_by_phone_number_id_via_the_index(client):
    _seed(client.app, tenant="beta", recipient="+966555555555", status="sent",
          phone_number_id="109998887776")
    r = client.get("/internal/whatsapp/deliveries?phone_number_id=109998887776", headers=_tok())
    assert r.status_code == 200 and r.json()["tenant"] == "beta"
    assert len(r.json()["deliveries"]) == 1


def test_neither_tenant_nor_known_number_is_a_400(client):
    assert client.get("/internal/whatsapp/deliveries", headers=_tok()).status_code == 400
    # an unknown number resolves to no tenant → refused, never a cross-tenant scan
    assert client.get("/internal/whatsapp/deliveries?phone_number_id=unknown",
                      headers=_tok()).status_code == 400
