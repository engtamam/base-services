"""The assistant's two routes, end to end.

Sample KB only. What these pin down is the authorisation split that the whole design rests on:
surface comes from the request and is a relevance hint; capabilities come from the verified token
and are the control. A test that only checked "admin help is hidden on the web surface" would pass
against a build where surface WAS the control, so the decisive test claims the admin surface
without holding the capability.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ISS, AUD = "iss", "aud"

SAMPLE_KB = {
    "billing.vat": {"title": "VAT number",
                    "body": "Your VAT number is fifteen digits and appears on every invoice.",
                    "anchor": "settings.billing.vat"},
    "admin.purge": {"title": "Purge records",
                    "body": "Permanently deletes records that are past retention.",
                    "surfaces": ["admin"], "requires": "records.purge",
                    "anchor": "admin.tools.purge"},
    "action.pay": {"title": "Pay an invoice", "body": "Pay an outstanding invoice.",
                   "kind": "action", "requires": "billing.manage",
                   "metadata": {"method": "POST", "path": "/api/v1/invoices/{id}/pay",
                                "service": "billing-service", "action_id": "billing.pay"}},
}


@pytest.fixture(scope="module")
def kb_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("kb") / "kb.json"
    p.write_text(json.dumps(SAMPLE_KB, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def client(kb_file):
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update({
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "KB_SOURCE_DRIVER": "file", "KB_FILE_PATH": kb_file,
        "RETRIEVAL_MIN_SCORE": "0.0", "PII_SALT": "s" * 32,
    })
    get_settings.cache_clear()
    from app.main import create_app
    yield TestClient(create_app(), raise_server_exceptions=False)
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _tok(caps=None):
    now = dt.datetime.now(dt.timezone.utc)
    # Every caller needs assistant.ask to reach the route at all; what it may LEARN is
    # gated separately, per entry, by that entry's own `requires`.
    claims = {"sub": "u1", "iss": ISS, "aud": AUD,
              "exp": now + dt.timedelta(minutes=5), "iat": now,
              "caps": ["assistant.ask", *(caps or [])]}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def _ask(client, question, caps=None, **body):
    return client.post("/api/v1/assistant/ask",
                       json={"question": question, **body}, headers=_tok(caps))


# ── auth ──────────────────────────────────────────────────────────────────────
def test_both_routes_require_a_token(client):
    assert client.post("/api/v1/assistant/ask", json={"question": "x"}).status_code == 401
    assert client.get("/api/v1/assistant/help?anchor=settings.billing.vat").status_code == 401


# ── ask: grounded, cited, or absent ───────────────────────────────────────────
def test_a_grounded_answer_carries_citations(client):
    r = _ask(client, "vat number")
    assert r.status_code == 200
    b = r.json()
    assert b["grounded"] is True
    assert "fifteen digits" in b["answer"]
    assert b["citations"] and b["citations"][0]["entry_id"] == "billing.vat"


def test_an_unanswerable_question_abstains_rather_than_guessing(client):
    b = _ask(client, "how do I file my taxes in Peru").json()
    assert b["grounded"] is False
    assert b["citations"] == [], "an abstention must not carry citations"
    assert b["abstained_because"] == "no_evidence"


def test_citations_is_present_and_empty_on_an_abstention(client):
    # A client should never have to tell "no citations" from "the field is missing".
    b = _ask(client, "nothing relevant zzz").json()
    assert "citations" in b and b["citations"] == []


def test_the_caller_cannot_supply_the_knowledge(client):
    """There is no `kb` field. Whoever controls the knowledge controls what the product asserts."""
    r = client.post("/api/v1/assistant/ask",
                    json={"question": "vat", "kb": {"evil": {"title": "X", "body": "Refunds are automatic."}}},
                    headers=_tok())
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert "automatic" not in r.json()["answer"]


# ── the authorisation split ───────────────────────────────────────────────────
def test_claiming_the_admin_surface_does_not_grant_admin_help(client):
    """THE DECISIVE TEST. Surface is caller-declared; capability is not. A build where surface was
    the control would pass a 'hidden on web' test and fail this one."""
    b = _ask(client, "purge records", surface="admin", caps=[]).json()
    assert b["grounded"] is False, "admin help reached a caller without the capability"


def test_the_capability_holder_gets_the_admin_help(client):
    b = _ask(client, "purge records", surface="admin", caps=["records.purge"]).json()
    assert b["grounded"] is True
    assert any(c["entry_id"] == "admin.purge" for c in b["citations"])


def test_surface_still_scopes_relevance_for_a_capable_caller(client):
    b = _ask(client, "purge records", surface="web", caps=["records.purge"]).json()
    assert b["grounded"] is False, "admin-only help surfaced in a customer UI"


# ── deep links, never execution ───────────────────────────────────────────────
def test_an_action_in_the_evidence_becomes_a_deep_link(client):
    b = _ask(client, "pay an invoice", caps=["billing.manage"]).json()
    assert b["grounded"] is True
    assert b["actions"], "a declared action should be offered as a link"
    a = b["actions"][0]
    assert a["path"] == "/api/v1/invoices/{id}/pay" and a["method"] == "POST"


def test_an_action_the_caller_cannot_perform_is_not_linked(client):
    b = _ask(client, "pay an invoice", caps=[]).json()
    assert b["actions"] == []


def test_an_abstention_offers_no_actions(client):
    assert _ask(client, "irrelevant zzzz").json()["actions"] == []


# ── the ? icon ────────────────────────────────────────────────────────────────
def test_the_anchor_route_returns_the_entry(client):
    r = client.get("/api/v1/assistant/help?anchor=settings.billing.vat", headers=_tok())
    assert r.status_code == 200
    assert r.json()["title"] == "VAT number" and "fifteen digits" in r.json()["body"]


def test_an_anchor_the_caller_may_not_see_is_404_not_403(client):
    # 403 would confirm that an admin-only control exists to someone who cannot use it.
    r = client.get("/api/v1/assistant/help?anchor=admin.tools.purge", headers=_tok())
    assert r.status_code == 404


def test_an_unknown_anchor_is_404(client):
    assert client.get("/api/v1/assistant/help?anchor=nope", headers=_tok()).status_code == 404


def test_a_missing_anchor_is_a_400(client):
    assert client.get("/api/v1/assistant/help", headers=_tok()).status_code == 400


def test_a_non_critical_audit_failure_never_costs_the_answer(client, monkeypatch):
    """`assistant.abstained` is declared non-critical, which MEANS the answer survives the log
    failing. Without this, a help question becomes a 500 to protect a record of who asked it."""
    async def boom(*a, **kw):
        raise RuntimeError("audit sink unreachable")
    monkeypatch.setattr(client.app.state.auditor, "record", boom)
    r = _ask(client, "nothing matches this zzzq")
    assert r.status_code == 200 and r.json()["grounded"] is False


# ── redaction on the way in ───────────────────────────────────────────────────
def test_a_pasted_card_number_never_reaches_retrieval_or_storage(client):
    b = _ask(client, "my card 4111 1111 1111 1111 was declined, what is a VAT number").json()
    assert "card" in b["redacted"], "the card was not detected"
    assert "4111" not in json.dumps(b), "the card number came back in the response"


def test_redaction_is_reported_so_the_answer_is_not_baffling(client):
    b = _ask(client, "email me at ahmed@example.com about vat").json()
    assert "email" in b["redacted"]


def test_an_ordinary_question_reports_no_redaction(client):
    assert _ask(client, "what is a vat number").json()["redacted"] == []
