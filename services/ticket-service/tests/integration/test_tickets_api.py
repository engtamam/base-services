"""Ticket routes end-to-end — multi-tenant isolation, the enforced workflow, operator search (53.4).

The load-bearing tests are the isolation ones: a customer sees only their own, tenant comes from the
token (never the body), and one tenant cannot read another's ticket.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update(
        ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, PII_SALT="s" * 32,
        TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="2000")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def app():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _tok(sub, caps, tenant="acme"):
    now = dt.datetime.now(dt.timezone.utc)
    return {"Authorization": "Bearer " + jwt.encode(
        {"sub": sub, "iat": now, "exp": now + dt.timedelta(minutes=5), "tenant": tenant,
         "caps": caps}, SECRET, algorithm="HS256")}


def _customer(sub="ada@x.com", tenant="acme"):
    return _tok(sub, ["tickets.self"], tenant)


def _operator(sub="op1", tenant="acme"):
    return _tok(sub, ["tickets.manage"], tenant)


def _open(app, headers, **over):
    body = {"type": "feedback", "subject": "Header overlaps",
            "body": "the header overlaps on mobile", "ref": {"kind": "document", "id": "tpl_1"}}
    body.update(over)
    return app.post("/api/v1/tickets", headers=headers, json=body)


def test_workflows_are_exposed(app):
    r = app.get("/api/v1/workflows", headers=_customer())
    assert r.status_code == 200
    keys = [t["key"] for t in r.json()["types"]]
    assert "feedback" in keys and "support" in keys


def test_a_customer_opens_a_feedback_ticket(app):
    r = _open(app, _customer())
    assert r.status_code == 201
    t = r.json()
    assert t["status"] == "new" and t["type"] == "feedback"
    assert "ada@x.com" not in t["raiser_ref"]          # pseudonymised


def test_feedback_requires_a_ref(app):
    r = _open(app, _customer(), ref=None)
    assert r.status_code == 422


def test_a_customer_sees_only_their_own(app):
    _open(app, _customer(sub="alice@x.com"))
    _open(app, _customer(sub="bob@x.com"))
    mine = app.get("/api/v1/tickets", headers=_customer(sub="alice@x.com")).json()["tickets"]
    assert mine and all(t["raiser_ref"] for t in mine)
    # bob's ticket is not in alice's list
    alice_ref = mine[0]["raiser_ref"]
    assert all(t["raiser_ref"] == alice_ref for t in mine)


def test_another_tenant_cannot_read_the_ticket(app):
    tid = _open(app, _customer(sub="carol@x.com", tenant="acme")).json()["id"]
    r = app.get(f"/api/v1/tickets/{tid}", headers=_customer(sub="mallory@x.com", tenant="evil"))
    assert r.status_code == 404


def test_the_workflow_is_enforced_over_http(app):
    tid = _open(app, _customer()).json()["id"]
    # customer cannot transition (needs tickets.manage)
    assert app.post(f"/api/v1/tickets/{tid}/status", headers=_customer(),
                    json={"to": "triaged"}).status_code == 403
    # operator: illegal jump refused
    assert app.post(f"/api/v1/tickets/{tid}/status", headers=_operator(),
                    json={"to": "resolved"}).status_code == 409
    # operator: legal move works
    assert app.post(f"/api/v1/tickets/{tid}/status", headers=_operator(),
                    json={"to": "triaged"}).status_code == 200


def test_operator_sees_the_tenant_wide_inbox_and_can_filter(app):
    _open(app, _customer(sub="dave@x.com"))
    r = app.get("/api/v1/tickets/operator/tickets?status=new", headers=_operator())
    assert r.status_code == 200 and len(r.json()["tickets"]) >= 1


def test_operator_list_embeds_per_ticket_kpis(app):
    # The admin dashboard aggregates SLA metrics (avg first-response, unresolved > 48h) over this
    # already-gated read, so each row must carry its derived KPIs alongside the raw ticket fields.
    tid = _open(app, _customer(sub="kpi@x.com")).json()["id"]
    # an operator's first reply is the first real response → first_response_seconds becomes computable
    app.post(f"/api/v1/tickets/{tid}/replies", headers=_operator(), json={"body": "on it"})
    rows = app.get("/api/v1/tickets/operator/tickets", headers=_operator()).json()["tickets"]
    row = next(r for r in rows if r["id"] == tid)
    assert "kpis" in row, "each operator-list row embeds ticket_kpis"
    k = row["kpis"]
    assert {"first_response_seconds", "resolution_seconds", "status"}.issubset(k)
    assert k["first_response_seconds"] is not None      # an operator reply landed → SLA is derivable
    assert k["operator_reply_count"] >= 1
    assert k["status"] == row["status"]                 # kpis.status mirrors the row it rode in on
    # a ticket with no operator reply yet has an OPEN (None) first-response, not a fabricated zero
    tid2 = _open(app, _customer(sub="kpi2@x.com")).json()["id"]
    rows2 = app.get("/api/v1/tickets/operator/tickets", headers=_operator()).json()["tickets"]
    row2 = next(r for r in rows2 if r["id"] == tid2)
    assert row2["kpis"]["first_response_seconds"] is None
    assert row2["kpis"]["resolution_seconds"] is None


def test_history_records_who_did_what(app):
    tid = _open(app, _customer()).json()["id"]
    app.post(f"/api/v1/tickets/{tid}/replies", headers=_customer(), json={"body": "any update?"})
    app.post(f"/api/v1/tickets/{tid}/status", headers=_operator(), json={"to": "triaged"})
    ev = app.get(f"/api/v1/tickets/{tid}/history", headers=_operator()).json()["events"]
    assert [e["kind"] for e in ev] == ["created", "replied", "status_changed"]
