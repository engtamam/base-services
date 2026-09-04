"""Internal-token READ twins for the (cross-repo) case-timeline investigation tool.

GET /internal/compliance/consent-history and GET /internal/compliance/dsar are internal-tier twins
of the user-tier reads. An internal caller acts FOR a tenant, so `tenant` is supplied in the request
(REQUIRED) and scopes every read — never widened past it. Both require the internal token; the DSAR
twin is filtered to the one subject asked for, never the whole tenant's list.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"
ITOKEN = "t" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "SECRETS_KEK": "unit-test-kek", "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(tenant: str, *caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "operator", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "tenant": tenant, "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _internal() -> dict:
    return {"X-Internal-Token": ITOKEN}


@pytest.fixture(scope="module")
def app():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


# ── consent-history twin ──────────────────────────────────────────────────────
def test_consent_history_requires_the_internal_token(app):
    assert app.get("/internal/compliance/consent-history?tenant=acme&subject=u1").status_code == 401
    assert app.get("/internal/compliance/consent-history?tenant=acme&subject=u1",
                   headers=_auth("acme", "compliance.read")).status_code == 401


def test_consent_history_requires_tenant_and_subject(app):
    assert app.get("/internal/compliance/consent-history?subject=u1", headers=_internal()).status_code == 422
    assert app.get("/internal/compliance/consent-history?tenant=acme", headers=_internal()).status_code == 422


def test_consent_history_returns_the_subjects_grants(app):
    app.post("/api/v1/compliance/consent/grant",
             json={"subject": "u1", "purpose": "marketing"},
             headers=_auth("acme", "compliance.manage"))
    r = app.get("/internal/compliance/consent-history?tenant=acme&subject=u1", headers=_internal())
    assert r.status_code == 200
    # same shape as the user-tier /consent/history read
    user = app.get("/api/v1/compliance/consent/history?subject=u1",
                   headers=_auth("acme", "compliance.read"))
    assert r.json() == user.json()


# ── dsar twin (filtered to one subject) ───────────────────────────────────────
def test_dsar_requires_the_internal_token(app):
    assert app.get("/internal/compliance/dsar?tenant=acme&subject=u1").status_code == 401


def test_dsar_is_filtered_to_the_one_subject(app):
    app.post("/api/v1/compliance/dsar", json={"subject": "alice@example.com", "type": "access"},
             headers=_auth("acme", "compliance.manage"))
    app.post("/api/v1/compliance/dsar", json={"subject": "bob@example.com", "type": "access"},
             headers=_auth("acme", "compliance.manage"))
    r = app.get("/internal/compliance/dsar?tenant=acme&subject=alice@example.com", headers=_internal())
    assert r.status_code == 200
    subjects = {d["subject"] for d in r.json()["dsars"]}
    assert subjects == {"alice@example.com"}  # bob's request never leaks into alice's timeline
    assert r.json()["count"] == len(r.json()["dsars"])


# ── legal/outstanding twin (identity's enforcement source) ────────────────────
_LEGAL_CONTENT = {"title": {"en": "Terms"}, "blocks": [{"section": {"text": {"en": "Be nice."}}}], "values": {}}


def _publish_terms(app, tenant, doc_id, version):
    manage = _auth(tenant, "compliance.manage")
    assert app.put(f"/api/v1/compliance/legal/{doc_id}/versions/{version}",
                   json={"content": _LEGAL_CONTENT, "requires_acceptance": True},
                   headers=manage).status_code in (200, 201)
    assert app.post(f"/api/v1/compliance/legal/{doc_id}/versions/{version}/publish",
                    headers=manage).status_code == 200


def test_legal_outstanding_requires_the_internal_token(app):
    assert app.get("/internal/compliance/legal/outstanding?tenant=acme&subject=u1").status_code == 401
    assert app.get("/internal/compliance/legal/outstanding?tenant=acme&subject=u1",
                   headers=_auth("acme", "compliance.self")).status_code == 401


def test_legal_outstanding_requires_tenant_and_subject(app):
    assert app.get("/internal/compliance/legal/outstanding?subject=u1", headers=_internal()).status_code == 422
    assert app.get("/internal/compliance/legal/outstanding?tenant=acme", headers=_internal()).status_code == 422


def test_legal_outstanding_reports_a_required_doc(app):
    # A REQUIRED published doc is outstanding for a subject who never accepted it.
    _publish_terms(app, "legaltenant", "terms", "1.0")
    r = app.get("/internal/compliance/legal/outstanding?tenant=legaltenant&subject=whoever@example.com",
                headers=_internal())
    assert r.status_code == 200
    assert [o["doc_id"] for o in r.json()["outstanding"]] == ["terms"]


def test_legal_outstanding_matches_the_subject_that_accepts(app):
    # The load-bearing consistency test: identity gates on token `sub`; acceptance is recorded against
    # the caller's token `sub`; the two MUST be the same subject or enforcement never clears.
    _publish_terms(app, "tconsist", "privacy", "1.0")
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "owner@shop.sa", "iss": ISS, "aud": AUD, "caps": ["compliance.self"],
                      "tenant": "tconsist", "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    self_hdr = {"Authorization": f"Bearer {tok}"}

    before = app.get("/internal/compliance/legal/outstanding?tenant=tconsist&subject=owner@shop.sa",
                     headers=_internal())
    assert [o["doc_id"] for o in before.json()["outstanding"]] == ["privacy"]

    assert app.post("/api/v1/compliance/me/legal/privacy/accept", headers=self_hdr).status_code == 200

    after = app.get("/internal/compliance/legal/outstanding?tenant=tconsist&subject=owner@shop.sa",
                    headers=_internal())
    assert after.json()["outstanding"] == []   # acceptance by the same subject clears the internal read


# ── legal list + history twins (dispute-report generator's backend source) ────
def test_legal_list_and_history_twins_require_token_and_tenant(app):
    assert app.get("/internal/compliance/legal").status_code == 401
    assert app.get("/internal/compliance/legal", headers=_internal()).status_code == 422
    assert app.get("/internal/compliance/legal/terms/history").status_code == 401
    assert app.get("/internal/compliance/legal/terms/history", headers=_internal()).status_code == 422


def test_legal_list_and_history_twins_match_the_user_tier(app):
    _publish_terms(app, "treport", "terms", "1.0")
    _publish_terms(app, "treport", "terms", "2.0")   # a second version -> history has two

    r = app.get("/internal/compliance/legal?tenant=treport", headers=_internal())
    assert r.status_code == 200
    user = app.get("/api/v1/compliance/legal", headers=_auth("treport", "compliance.read"))
    assert r.json() == user.json()                    # same shape/content as the user-tier read
    assert any(d.get("doc_id") == "terms" for d in r.json()["documents"])

    h = app.get("/internal/compliance/legal/terms/history?tenant=treport", headers=_internal())
    assert h.status_code == 200
    uh = app.get("/api/v1/compliance/legal/terms/history", headers=_auth("treport", "compliance.read"))
    assert h.json() == uh.json()
    assert len(h.json()["versions"]) == 2             # both published versions


def test_legal_history_twin_is_tenant_scoped(app):
    _publish_terms(app, "tenantA", "terms", "1.0")
    # a different tenant asking for the same doc_id sees its OWN (empty) history, never tenantA's
    other = app.get("/internal/compliance/legal/terms/history?tenant=tenantB", headers=_internal())
    assert other.status_code == 200 and other.json()["versions"] == []
