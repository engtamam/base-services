"""POST /internal/compliance/consent/record — a subject recording its OWN live consent decision.

The gap this fills: /consent/grant and /consent/withdraw require an admin JWT, so only an operator
could record a decision. A service capturing an END CUSTOMER'S own decision (a WhatsApp button tap)
had nowhere to write it, and consent-check's UNKNOWN verdict stayed unresolved. This route is
service-to-service (internal token, the same boundary as consent-check), basis is always `self`, and
the decision flows into the SAME consent store + proof chain as the operator routes — so consent-check
sees it immediately.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": "h" * 40,
        "RATELIMIT_MAX": "1000", "SECRETS_KEK": "unit-test-kek", "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _internal() -> dict:
    return {"X-Internal-Token": ITOKEN}


@pytest.fixture()
def app():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def test_requires_the_internal_token(app):
    r = app.post("/internal/compliance/consent/record",
                 json={"tenant": "acme", "subject": "+96650", "purpose": "service", "granted": True})
    assert r.status_code == 401


def test_a_granted_decision_is_visible_to_consent_check(app):
    body = {"tenant": "acme", "subject": "+96650", "purpose": "service", "granted": True,
            "method": "whatsapp_interactive"}
    r = app.post("/internal/compliance/consent/record", headers=_internal(), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["granted"] is True
    # consent-check (the hot path the bot gates on) now returns allowed, not UNKNOWN
    chk = app.get("/internal/compliance/consent-check?tenant=acme&subject=%2B96650&purpose=service",
                  headers=_internal()).json()
    assert chk["allowed"] is True


def test_a_refusal_is_recorded_as_a_withdrawal_and_denies(app):
    # grant, then the subject taps "end conversation" → recorded as not-granted
    g = {"tenant": "acme", "subject": "+96651", "purpose": "service", "granted": True}
    app.post("/internal/compliance/consent/record", headers=_internal(), json=g)
    r = app.post("/internal/compliance/consent/record", headers=_internal(),
                 json={**g, "granted": False})
    assert r.status_code == 200 and r.json()["granted"] is False
    chk = app.get("/internal/compliance/consent-check?tenant=acme&subject=%2B96651&purpose=service",
                  headers=_internal()).json()
    assert chk["allowed"] is False


def test_it_leaves_a_proof_with_the_subject_as_actor_and_the_capture_method(app):
    app.post("/internal/compliance/consent/record", headers=_internal(),
             json={"tenant": "acme", "subject": "+96652", "purpose": "service", "granted": True,
                   "method": "whatsapp_interactive"})
    hist = app.get("/internal/compliance/consent-history?tenant=acme&subject=%2B96652",
                   headers=_internal()).json()
    proofs = hist["proofs"]
    # the proof records HOW it was captured …
    assert any(p.get("method") == "whatsapp_interactive" for p in proofs)
    # … and the ACTOR is the subject's PSEUDONYM (subject_ref), never the raw phone number
    actors = [p.get("actor", "") for p in proofs]
    assert all(a and a != "+96652" for a in actors)
    assert any(a.startswith("sub_") for a in actors)


def test_one_tenant_cannot_be_recorded_under_anothers_scope(app):
    # the decision is scoped strictly to the tenant in the body
    app.post("/internal/compliance/consent/record", headers=_internal(),
             json={"tenant": "shopA", "subject": "+96653", "purpose": "service", "granted": True})
    other = app.get("/internal/compliance/consent-check?tenant=shopB&subject=%2B96653&purpose=service",
                    headers=_internal()).json()
    assert other["allowed"] is False   # shopB never saw a grant for this subject
