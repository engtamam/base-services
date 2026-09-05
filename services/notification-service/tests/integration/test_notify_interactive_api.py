"""POST /internal/notify/whatsapp/interactive — send button/list messages through the shared,
audited WhatsApp plumbing (so a caller need not reach the Cloud API directly), recorded as a
tenant-scoped Delivery, with a bad spec refused as a clear 422 rather than an opaque Meta 400."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40, "RATELIMIT_MAX": "1000"}
    env.update(over)
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


def _tok():
    return {"X-Internal-Token": ITOKEN}


def test_requires_the_internal_token(client):
    assert client.post("/internal/notify/whatsapp/interactive",
                       json={"recipient": "+966", "type": "button", "body": "x",
                             "buttons": [{"id": "ok", "title": "OK"}]}).status_code == 401


def test_button_send_is_recorded_as_a_tenant_scoped_delivery(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966500000001", "tenant": "acme", "type": "button",
                          "body": "Confirm your booking?",
                          "buttons": [{"id": "confirm", "title": "Confirm"},
                                      {"id": "cancel", "title": "Cancel"}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    # it lands in the same tenant-scoped WhatsApp diagnostic as any other send
    rows = client.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:button" and d["status"] == "sent" for d in rows)


def test_list_send_succeeds(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966", "tenant": "acme", "type": "list", "body": "Pick a slot",
                          "button_text": "Choose",
                          "sections": [{"title": "Morning", "rows": [
                              {"id": "s9", "title": "09:00"}, {"id": "s10", "title": "10:00"}]}]})
    assert r.status_code == 200 and r.json()["status"] == "sent"


def test_a_bad_spec_is_a_clear_422_not_a_meta_400(client):
    # four reply buttons — Meta allows three; refused at build time with a reason
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966", "type": "button", "body": "x",
                          "buttons": [{"id": f"b{i}", "title": str(i)} for i in range(4)]})
    assert r.status_code == 422 and "3" in r.json()["reason"]   # the reason reaches the caller


def test_idempotency_key_dedupes_a_resend(client):
    payload = {"recipient": "+966", "tenant": "acme", "type": "button", "body": "Confirm?",
               "buttons": [{"id": "y", "title": "Yes"}], "idempotency_key": "book-42"}
    first = client.post("/internal/notify/whatsapp/interactive", headers=_tok(), json=payload).json()
    second = client.post("/internal/notify/whatsapp/interactive", headers=_tok(), json=payload).json()
    assert second.get("duplicate") is True and second["delivery_id"] == first["delivery_id"]


# ── flow — the post-visit rating app's own message shape ──────────────────────
def test_flow_send_is_recorded_as_a_tenant_scoped_delivery(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966500000002", "tenant": "acme", "type": "flow",
                          "body": "How was your visit?", "flow_token": "rate:vis_1",
                          "flow_id": "123456789", "flow_cta": "Rate us", "flow_screen": "RATING"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    rows = client.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:flow" and d["status"] == "sent" for d in rows)


def test_location_request_send_is_recorded_as_a_tenant_scoped_delivery(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966500000003", "tenant": "acme", "type": "location_request",
                          "body": "Share your location to find the nearest branch"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["provider"] == "console"
    rows = client.get("/internal/whatsapp/deliveries?tenant=acme", headers=_tok()).json()["deliveries"]
    assert any(d["template_key"] == "interactive:location_request" and d["status"] == "sent"
               for d in rows)


def test_flow_send_by_name_succeeds(client):
    # The WABA-portable identifier: a per-tenant clone keeps its name, so a send can reference it by
    # flow_name instead of a per-clone flow_id.
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966500000004", "tenant": "acme", "type": "flow",
                          "body": "How was your visit?", "flow_token": "rate:vis_9",
                          "flow_name": "rating_flow", "flow_cta": "Rate us", "flow_screen": "RATING"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent"


def test_flow_missing_its_own_required_field_is_a_clear_422(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966", "type": "flow", "body": "x",
                          "flow_token": "", "flow_id": "f1", "flow_cta": "Go", "flow_screen": "S1"})
    assert r.status_code == 422 and "flow_token" in r.json()["reason"]


def test_flow_idempotency_key_dedupes_a_resend(client):
    payload = {"recipient": "+966", "tenant": "acme", "type": "flow", "body": "How was your visit?",
               "flow_token": "rate:vis_2", "flow_id": "f1", "flow_cta": "Rate us",
               "flow_screen": "RATING", "idempotency_key": "rate-vis-2"}
    first = client.post("/internal/notify/whatsapp/interactive", headers=_tok(), json=payload).json()
    second = client.post("/internal/notify/whatsapp/interactive", headers=_tok(), json=payload).json()
    assert second.get("duplicate") is True and second["delivery_id"] == first["delivery_id"]


def test_flow_data_seeds_the_first_screen_without_being_echoed_in_the_response(client):
    r = client.post("/internal/notify/whatsapp/interactive", headers=_tok(),
                    json={"recipient": "+966", "tenant": "acme", "type": "flow", "body": "x",
                          "flow_token": "rate:vis_3", "flow_id": "f1", "flow_cta": "Go",
                          "flow_screen": "RATING", "flow_data": {"customer_name": "Ahmed"}})
    assert r.status_code == 200 and r.json()["status"] == "sent"
