"""The INTERNAL WhatsApp template WRITE twins + the rate-limited drain.

A backend service is not a user and holds no session, so registering/submitting a shop's template
library — which belongs to the moment an account connects, and is rate-limited by Meta — must run
service-to-service. These are the token-authed twins of the two user routes, with the tenant in the
body, plus submit-pending: the backend job that uploads a whole library within Meta's 100/hour cap.
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


def _register(client, name, **over):
    payload = {"tenant": "acme", "name": name, "language": "en", "category": "UTILITY",
               "body": "hi {{1}}", "variables": ["who"], "examples": ["Sara"]}
    payload.update(over)
    return client.post("/internal/whatsapp/templates", headers=_tok(), json=payload)


def test_write_twins_require_the_internal_token():
    c = TestClient(create_app())
    assert c.post("/internal/whatsapp/templates",
                  json={"tenant": "acme", "name": "otp", "language": "en",
                        "category": "UTILITY"}).status_code == 401
    assert c.post("/internal/whatsapp/templates/otp/en/submit",
                  json={"tenant": "acme"}).status_code == 401
    assert c.post("/internal/whatsapp/templates/submit-pending?tenant=acme").status_code == 401


def test_register_twin_creates_a_pending_template_from_the_body_tenant(client):
    r = _register(client, "otp")
    assert r.status_code == 201
    body = r.json()
    assert body["tenant"] == "acme" and body["status"] == "pending"      # tenant from BODY
    assert body["provider_template_id"] == ""                             # not yet handed to Meta


def test_submit_twin_hands_it_to_meta(client):
    _register(client, "otp")
    r = client.post("/internal/whatsapp/templates/otp/en/submit", headers=_tok(),
                    json={"tenant": "acme"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"          # console manager auto-approves
    assert r.json()["provider_template_id"]          # a Meta (console) id is recorded


def test_submit_pending_drains_the_library_and_is_idempotent(client):
    for n in ("a", "b", "c"):
        _register(client, n)
    r = client.post("/internal/whatsapp/templates/submit-pending?tenant=acme", headers=_tok())
    assert r.status_code == 200
    out = r.json()
    assert out["submitted"] == 3 and out["remaining"] == 0
    # a second tick finds nothing never-submitted left — no double-submit
    out2 = client.post("/internal/whatsapp/templates/submit-pending?tenant=acme", headers=_tok()).json()
    assert out2["submitted"] == 0 and out2["pending_unsent"] == 0


def test_submit_pending_requires_a_tenant(client):
    assert client.post("/internal/whatsapp/templates/submit-pending", headers=_tok()).status_code == 422


def test_reregister_unchanged_does_not_reset_an_approved_template(client):
    # the approved-guard the escalation calls out: a no-op re-register must NOT un-approve.
    _register(client, "otp")
    client.post("/internal/whatsapp/templates/otp/en/submit", headers=_tok(), json={"tenant": "acme"})
    assert _register(client, "otp").json()["status"] == "approved"   # unchanged content stays approved
    # a real content change DOES re-open review
    assert _register(client, "otp", body="hi {{1}} !!").json()["status"] == "pending"
