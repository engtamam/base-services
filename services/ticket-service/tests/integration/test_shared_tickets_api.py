"""Scoped cross-service ticket visibility, end-to-end.

A shop consents to ONE outside party (a marketer) reading a scoped view of its tickets; that party
reads structure + KPIs over the internal token, never content, never another tenant, never without a
live grant. This is the primitive affiliate-service (spec §13) keys a support-performance stipend on.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


@pytest.fixture(scope="module")
def app():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, PII_SALT="s" * 32,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="5000")
    get_settings.cache_clear()
    from app.main import create_app
    try:
        yield TestClient(create_app(), raise_server_exceptions=False)
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _tok(sub, caps, tenant="acme"):
    now = dt.datetime.now(dt.timezone.utc)
    return {"Authorization": "Bearer " + jwt.encode(
        {"sub": sub, "iat": now, "exp": now + dt.timedelta(minutes=5), "tenant": tenant, "caps": caps},
        SECRET, algorithm="HS256")}


def _seed_ticket(app, tenant="acme", subject="Header overlaps on mobile", body="secret customer note"):
    cust, op = _tok("ada@x.com", ["tickets.self"], tenant), _tok("op1", ["tickets.manage"], tenant)
    tid = app.post("/api/v1/tickets", headers=cust, json={
        "type": "feedback", "subject": subject, "body": body,
        "ref": {"kind": "document", "id": "tpl_1"}}).json()["id"]
    app.post(f"/api/v1/tickets/{tid}/replies", headers=op, json={"body": "on it"})   # first response
    app.post(f"/api/v1/tickets/{tid}/escalate", headers=op, json={"level": "tier2"})
    return tid


def _grant(app, tenant="acme", grantee="mkr1", **body):
    return app.post("/internal/tickets/shares", headers=IT,
                    json={"tenant": tenant, "grantee": grantee, **body})


# ── the grant gate ──────────────────────────────────────────────────────────────
def test_without_a_grant_the_read_refuses_without_leaking_existence(app):
    _seed_ticket(app)
    r = app.get("/internal/tickets?tenant=acme&grantee=nobody", headers=IT)
    assert r.status_code == 409 and r.json()["error"] == "NO_ACTIVE_GRANT"


def test_a_granted_party_sees_the_tenants_tickets(app):
    _seed_ticket(app)
    assert _grant(app, grantee="mkr1").status_code == 200
    inv = app.get("/internal/tickets?tenant=acme&grantee=mkr1", headers=IT).json()
    assert len(inv["tickets"]) >= 1 and all(t["type"] == "feedback" for t in inv["tickets"])


def test_a_revoked_grant_grants_nothing(app):
    _seed_ticket(app)
    _grant(app, grantee="mkr_rev")
    assert app.get("/internal/tickets?tenant=acme&grantee=mkr_rev", headers=IT).status_code == 200
    assert app.delete("/internal/tickets/shares/mkr_rev?tenant=acme", headers=IT).json()["revoked"] is True
    assert app.get("/internal/tickets?tenant=acme&grantee=mkr_rev", headers=IT).status_code == 409


# ── the KPI read ──────────────────────────────────────────────────────────────────
def test_single_ticket_read_carries_derived_kpis(app):
    tid = _seed_ticket(app)
    _grant(app, grantee="mkr_kpi")
    k = app.get(f"/internal/tickets/{tid}?tenant=acme&grantee=mkr_kpi", headers=IT).json()["kpis"]
    assert k["escalated"] is True and k["escalation_level"] == "tier2"
    assert k["first_response_at"] and k["first_response_seconds"] is not None   # a real operator reply
    assert k["operator_reply_count"] == 1


# ── privacy: structure and performance, never content ─────────────────────────────
def test_no_subject_or_body_ever_crosses_the_wire(app):
    tid = _seed_ticket(app, subject="REFUND for order 9910 Ahmed", body="customer PII here")
    _grant(app, grantee="mkr_priv")
    for path in (f"/internal/tickets?tenant=acme&grantee=mkr_priv",
                 f"/internal/tickets/{tid}?tenant=acme&grantee=mkr_priv"):
        text = app.get(path, headers=IT).text
        for leaked in ("REFUND", "Ahmed", "customer PII", "subject"):
            assert leaked not in text, f"{leaked!r} leaked to a grantee"


# ── scope narrows, tenant isolates ────────────────────────────────────────────────
def test_scope_narrows_which_tickets_are_visible(app):
    tid = _seed_ticket(app)                              # type=feedback
    _grant(app, grantee="mkr_scope", scope={"type": ["support"]})   # only support, not feedback
    assert app.get("/internal/tickets?tenant=acme&grantee=mkr_scope", headers=IT).json()["tickets"] == []
    # an uncovered ticket answers 404 exactly like a missing one — the grant is not a probe
    assert app.get(f"/internal/tickets/{tid}?tenant=acme&grantee=mkr_scope", headers=IT).status_code == 404


def test_a_grant_for_one_tenant_never_reaches_another(app):
    _seed_ticket(app, tenant="acme")
    _seed_ticket(app, tenant="other")
    _grant(app, tenant="acme", grantee="mkr_iso")
    # the grant is (acme, mkr_iso); asking about tenant "other" needs its OWN grant -> refused
    assert app.get("/internal/tickets?tenant=other&grantee=mkr_iso", headers=IT).status_code == 409


# ── the token gate ────────────────────────────────────────────────────────────────
def test_every_share_route_requires_the_internal_token(app):
    assert app.get("/internal/tickets?tenant=acme&grantee=mkr1").status_code == 401
    assert app.post("/internal/tickets/shares", json={"tenant": "acme", "grantee": "x"}).status_code == 401
    assert app.get("/internal/tickets/shares?tenant=acme").status_code == 401
