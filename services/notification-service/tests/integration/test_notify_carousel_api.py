"""POST /internal/notify/whatsapp/carousel — send an approved carousel template with per-card values.

Business-initiated, through the shared audited/idempotent/tenant-scoped delivery plumbing. Driven end
to end here via the console driver (no Meta round-trip in dev).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture()
def client():
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def _cards():
    return [{"media_id": "m0", "media_type": "image", "body_params": ["Pizza"],
             "buttons": [{"sub_type": "quick_reply", "payload": "order:0"}]},
            {"media_id": "m1", "media_type": "image", "body_params": ["Burger"],
             "buttons": [{"sub_type": "quick_reply", "payload": "order:1"}]}]


def test_carousel_requires_the_internal_token():
    c = TestClient(create_app())
    assert c.post("/internal/notify/whatsapp/carousel",
                  json={"recipient": "+966", "template_name": "menu",
                        "cards": _cards()}).status_code == 401


def test_carousel_send_is_recorded_as_a_tenant_scoped_delivery(client):
    r = client.post("/internal/notify/whatsapp/carousel", headers=_tok(),
                    json={"recipient": "+966500000010", "tenant": "acme", "template_name": "menu",
                          "language": "en", "cards": _cards()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    rows = client.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "menu" and d["status"] == "sent" for d in rows)


def test_carousel_bad_card_is_a_clear_422(client):
    r = client.post("/internal/notify/whatsapp/carousel", headers=_tok(),
                    json={"recipient": "+966", "tenant": "acme", "template_name": "menu",
                          "cards": [{"media_type": "image", "buttons": []}]})   # no media_id
    assert r.status_code == 422


def test_carousel_idempotency_key_dedupes_a_resend(client):
    payload = {"recipient": "+966", "tenant": "acme", "template_name": "menu", "cards": _cards(),
               "idempotency_key": "car-1"}
    first = client.post("/internal/notify/whatsapp/carousel", headers=_tok(), json=payload).json()
    second = client.post("/internal/notify/whatsapp/carousel", headers=_tok(), json=payload).json()
    assert second.get("duplicate") is True and second["delivery_id"] == first["delivery_id"]
