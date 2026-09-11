"""GET /api/v1/whatsapp/flows/{name}/preview-json — the OWNER-facing twin of the internal flow-json
route, so the console can render a visual Flow preview. require_user (any signed-in shop), read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
_BOOKING_DEF = json.dumps({"steps": [{"dimension": "service"}, {"dimension": "date"},
                                     {"dimension": "confirm"}]})


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET,
                      RATELIMIT_MAX="1000", WHATSAPP_BOOKING_DEFINITION=_BOOKING_DEF)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _user(sub="u1", tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": sub, "tenant": tenant, "iat": now,
                      "exp": now + dt.timedelta(minutes=5)}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def test_owner_can_preview_a_configured_flow(client):
    r = client.get("/api/v1/whatsapp/flows/booking/preview-json", headers=_user())
    assert r.status_code == 200, r.text
    ids = {s["id"] for s in r.json()["screens"]}
    assert "SELECT" in ids and "CALENDAR" in ids and "CONFIRM" in ids


def test_requires_a_user_token(client):
    assert client.get("/api/v1/whatsapp/flows/booking/preview-json").status_code == 401


def test_unknown_flow_is_404(client):
    assert client.get("/api/v1/whatsapp/flows/nope/preview-json",
                      headers=_user()).status_code == 404


def test_a_non_booking_engine_is_404(client):
    # loyalty is registered but is not a slot-reservation engine → no companion booking JSON
    assert client.get("/api/v1/whatsapp/flows/loyalty/preview-json",
                      headers=_user()).status_code == 404


def test_configured_but_no_definition_is_404(client):
    # delivery has no WHATSAPP_DELIVERY_DEFINITION → engine registered, definition None
    assert client.get("/api/v1/whatsapp/flows/delivery/preview-json",
                      headers=_user()).status_code == 404
