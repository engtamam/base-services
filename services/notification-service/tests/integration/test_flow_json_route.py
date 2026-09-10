"""GET /internal/whatsapp/flow/{name}-json — the companion Flow JSON for ANY registered
slot-reservation engine, keyed by name (booking/delivery/visit/…), one route not one-per-Flow.
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings

IT = {"X-Internal-Token": "t" * 40}

_BOOKING_DEF = json.dumps({"steps": [{"dimension": "service"}, {"dimension": "date"},
                                     {"dimension": "confirm"}]})
_VISIT_DEF = json.dumps({"steps": [{"dimension": "phone", "input": "text"},
                                   {"dimension": "service", "multi": True},
                                   {"dimension": "confirm"}]})


@pytest.fixture()
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40,
                      WHATSAPP_BOOKING_DEFINITION=_BOOKING_DEF, WHATSAPP_VISIT_DEFINITION=_VISIT_DEF)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def test_booking_json_still_served_by_the_generic_route(client):
    r = client.get("/internal/whatsapp/flow/booking-json", headers=IT)
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["screens"]}
    assert "SELECT" in ids and "CALENDAR" in ids


def test_visit_json_is_served_by_name(client):
    r = client.get("/internal/whatsapp/flow/visit-json", headers=IT)
    assert r.status_code == 200
    js = r.json()
    ids = {s["id"] for s in js["screens"]}
    assert "INPUT" in ids and "SELECT_MULTI" in ids and "CALENDAR" not in ids   # no forced calendar


def test_unknown_flow_name_404(client):
    assert client.get("/internal/whatsapp/flow/nope-json", headers=IT).status_code == 404


def test_a_non_booking_engine_name_404(client):
    # loyalty is registered but is NOT a slot-reservation engine — it has no companion booking JSON.
    assert client.get("/internal/whatsapp/flow/loyalty-json", headers=IT).status_code == 404


def test_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/flow/booking-json").status_code == 401


def test_configured_but_no_definition_is_404(client):
    # delivery has no WHATSAPP_DELIVERY_DEFINITION set → engine registered, definition None → 404.
    assert client.get("/internal/whatsapp/flow/delivery-json", headers=IT).status_code == 404
