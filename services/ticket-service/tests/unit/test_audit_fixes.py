"""Regression tests for ticket-service audit findings.

#01 erasure redacts the legacy raw-id in `ref` (keeps the channel kind).
#02 inbound stores the sender's PSEUDONYM in `ref`, never the raw phone (see test_inbound_bridge).
#03 the per-type reply_capability is ENFORCED at the reply route (was parsed, never checked).
#04 the grantee-scoped list PAGES the tenant, so a covered ticket past the first page is not missed.
#05 inbound idempotency claim is ATOMIC (cas) — two deliveries of one message open one ticket.
#06 a state transition is ATOMIC (cas) — two concurrent moves cannot both apply from the same base.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import TicketErasure
from app.core.inbound_bridge import InboundBridge, InboundMessage
from app.core.pii import subject_ref
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry

SALT = "s" * 32
SECRET = "h" * 40

_TYPES = [{"key": "support", "label": {"en": "S"},
           "statuses": ["new", "open", "closed"], "start": "new",
           "transitions": {"new": ["open", "closed"], "open": ["closed"]},
           "terminal": ["closed"], "required_fields": [],
           "transition_capabilities": {"open": "tickets.manage", "closed": "tickets.manage"}}]


def run(c):
    return asyncio.run(c)


def _svc(st=None):
    st = st or MemoryDataStore()
    svc = TicketService(tickets=TicketStore(st), events=EventStore(st),
                        workflow=WorkflowRegistry.from_config(_TYPES), pii_salt=SALT)
    return st, svc


# ── 01: erasure drops a legacy raw ref-id, keeps the channel kind ─────────────
def test_erasure_redacts_legacy_ref_id_keeps_kind():
    st, svc = _svc()
    t = run(svc.open(tenant="acme", type="support", subject="hi", body="secret",
                     raiser="966500", ref={"kind": "whatsapp", "id": "966500"}))
    # simulate a legacy row whose ref.id is the raw phone (new rows already store a pseudonym)
    row = run(st.get("tickets", t.id)); row["ref"] = {"kind": "whatsapp", "id": "966500"}
    run(st.put("tickets", t.id, row))
    out = run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "966500"))
    after = run(st.get("tickets", t.id))
    assert after["ref"] == {"kind": "whatsapp"}       # id gone, kind kept
    assert after["subject"] == "[erased]"
    assert out["already_absent"] is False


# ── 05: two deliveries of one message open exactly one ticket (atomic claim) ───
def test_concurrent_inbound_ingests_once():
    st, svc = _svc()
    bridge = InboundBridge(service=svc, workflow=WorkflowRegistry.from_config(_TYPES),
                           store=st, ticket_type="support")
    msg = InboundMessage(channel="whatsapp", tenant="acme", sender="966500",
                         external_id="wamid.dup", text="hi", timestamp=1.0)

    async def _race():
        return await asyncio.gather(bridge.ingest(msg), bridge.ingest(msg))

    outcomes = run(_race())
    assert sorted(outcomes) == ["created", "skipped"]   # exactly one opened, the other deduped
    rows, _ = run(TicketStore(st).find(tenant="acme"))
    assert len(rows) == 1


# ── 06: two concurrent transitions cannot both apply from the same base ───────
def test_concurrent_transition_applies_once():
    st, svc = _svc()
    t = run(svc.open(tenant="acme", type="support", subject="hi", body="b", raiser="u"))
    caps = {"tickets.manage"}

    async def _race():
        return await asyncio.gather(
            svc.transition(tenant="acme", ticket_id=t.id, to="open", actor="op",
                           actor_caps=caps, is_operator=True),
            svc.transition(tenant="acme", ticket_id=t.id, to="closed", actor="op",
                           actor_caps=caps, is_operator=True),
            return_exceptions=True)

    run(_race())
    hist, _ = run(svc.history("acme", t.id))
    changes = [e for e in hist if e["kind"] == "status_changed"]
    # Each status_changed must build on the previous status, never two from "new" at once.
    froms = [e["data"]["from"] for e in changes]
    assert len(froms) == len(set(froms))               # no two moves share a base status
    final = run(svc.get("acme", t.id))
    assert final.status in ("open", "closed")


# ── 03 + 04: route-level (reply_capability enforcement, paged grantee list) ───
def _app(reply_cap="tickets.self", share_max=200):
    base = {"statuses": ["new", "open", "closed"], "start": "new",
            "transitions": {"new": ["open", "closed"], "open": ["closed"]},
            "terminal": ["closed"], "required_fields": [], "reply_capability": reply_cap,
            "transition_capabilities": {"open": "tickets.manage"},
            "notify": {"on": [], "template_prefix": "t"}}
    types = [{"key": "support", "label": {"en": "S", "ar": "س"}, **base},
             {"key": "other", "label": {"en": "O", "ar": "غ"}, **base}]
    import json as _json
    from app.config import get_settings
    os.environ.update(
        ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, PII_SALT=SALT,
        TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="5000",
        TICKET_TYPES_JSON=_json.dumps(types), TICKET_SHARE_LIST_MAX=str(share_max))
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _tok(sub, caps, tenant="acme"):
    now = dt.datetime.now(dt.timezone.utc)
    return {"Authorization": "Bearer " + jwt.encode(
        {"sub": sub, "iat": now, "exp": now + dt.timedelta(minutes=5), "tenant": tenant,
         "caps": caps}, SECRET, algorithm="HS256")}


@pytest.fixture
def restore_env():
    saved = dict(os.environ)
    yield
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def test_reply_capability_enforced_at_route(restore_env):
    app = _app(reply_cap="tickets.premium")
    # A raiser opens a ticket of a type that requires "tickets.premium" to reply.
    raiser = _tok("ada@x.com", ["tickets.self", "tickets.premium"])
    r = app.post("/api/v1/tickets", headers=raiser,
                 json={"type": "support", "subject": "s", "body": "b"})
    assert r.status_code == 201
    tid = r.json()["id"]
    # Same raiser WITHOUT the premium cap (still passes _SELF via tickets.self) is refused.
    weak = _tok("ada@x.com", ["tickets.self"])
    r2 = app.post(f"/api/v1/tickets/{tid}/replies", headers=weak, json={"body": "hi"})
    assert r2.status_code == 403                        # pre-fix: 201 (cap never checked)
    # With the premium cap it succeeds.
    r3 = app.post(f"/api/v1/tickets/{tid}/replies", headers=raiser, json={"body": "hi"})
    assert r3.status_code == 201


def test_shared_list_pages_past_the_first_page(restore_env):
    app = _app(reply_cap="tickets.self", share_max=2)
    internal = {"X-Internal-Token": "t" * 40}
    raiser = _tok("ada@x.com", ["tickets.self"])
    # Two COVERED (type=support) tickets are opened FIRST (oldest); then several UNCOVERED (type=other)
    # newer ones. Newest-first ordering fills page one entirely with uncovered rows, so a single-page
    # scan sees zero covered — the covered rows sit on a later page.
    covered_ids = {app.post("/api/v1/tickets", headers=raiser,
                            json={"type": "support", "subject": f"keep{i}", "body": "b"}).json()["id"]
                   for i in range(2)}
    for i in range(5):                                  # 5 uncovered, all newer than the covered pair
        app.post("/api/v1/tickets", headers=raiser,
                 json={"type": "other", "subject": f"n{i}", "body": "b"})
    g = app.post("/internal/tickets/shares", headers=internal,
                 json={"tenant": "acme", "grantee": "peer", "scope": {"type": ["support"]}})
    assert g.status_code in (200, 201)
    r = app.get("/internal/tickets", headers=internal, params={"tenant": "acme", "grantee": "peer"})
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["tickets"]}
    assert ids == covered_ids                           # pre-fix: empty (only page one, all uncovered)
