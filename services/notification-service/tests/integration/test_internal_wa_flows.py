"""POST /internal/whatsapp/flows/provision — the callable Flow-management primitive.

A Flow is WABA-scoped, so an adopter's connect job provisions one per shop (create-from-json or the
recommended clone-from-master) at the same step it registers templates, then references it by
flow_NAME. The route must be token-authed, idempotent by name, and reject a bad spec with a clear
422 — driven end to end here through the console driver (no Meta round-trip in dev).
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


def test_provision_requires_the_internal_token():
    c = TestClient(create_app())
    assert c.post("/internal/whatsapp/flows/provision",
                  json={"tenant": "acme", "name": "rating", "categories": ["SURVEY"],
                        "flow_json": "{}"}).status_code == 401


def test_provision_creates_and_publishes_from_flow_json(client):
    r = client.post("/internal/whatsapp/flows/provision", headers=_tok(),
                    json={"tenant": "acme", "name": "rating", "categories": ["SURVEY"],
                          "flow_json": '{"version":"3.0"}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["published"] and body["name"] == "rating"
    assert body["flow_id"]


def test_provision_is_idempotent_by_name(client):
    payload = {"tenant": "acme", "name": "rating", "categories": ["SURVEY"],
               "clone_flow_id": "master:1"}
    first = client.post("/internal/whatsapp/flows/provision", headers=_tok(), json=payload).json()
    second = client.post("/internal/whatsapp/flows/provision", headers=_tok(), json=payload).json()
    # NOTE: the console driver holds its name→id map per manager instance; within one app/process a
    # repeat ensure finds the existing Flow rather than minting a second id.
    assert first["created"] is True
    assert second["created"] is False and second["flow_id"] == first["flow_id"]


def test_provision_rejects_both_sources_with_422(client):
    r = client.post("/internal/whatsapp/flows/provision", headers=_tok(),
                    json={"tenant": "acme", "name": "r", "categories": ["SURVEY"],
                          "flow_json": "{}", "clone_flow_id": "x"})
    assert r.status_code == 422


def test_provision_rejects_a_bad_category_with_422(client):
    r = client.post("/internal/whatsapp/flows/provision", headers=_tok(),
                    json={"tenant": "acme", "name": "r", "categories": ["PROMO"],
                          "flow_json": "{}"})
    assert r.status_code == 422
