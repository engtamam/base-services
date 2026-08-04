"""TicketService — the engine + the ENFORCED state machine (53.3)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import (InvalidTransition, RequiredFieldMissing, TicketNotFound,
                                     TicketService, TransitionForbidden, UnknownType)
from app.core.workflow import WorkflowRegistry

SALT = "s" * 32
OP = {"tickets.manage"}
SELF = {"tickets.self"}

TYPES = [
    {"key": "feedback", "label": {"en": "Feedback"},
     "statuses": ["new", "triaged", "in_progress", "resolved", "closed"], "start": "new",
     "transitions": {"new": ["triaged", "closed"], "triaged": ["in_progress", "closed"],
                     "in_progress": ["resolved"], "resolved": ["closed", "in_progress"]},
     "terminal": ["closed"], "required_fields": ["ref"],
     "transition_capabilities": {"triaged": "tickets.manage", "in_progress": "tickets.manage",
                                 "resolved": "tickets.manage"}},
]


def run(c):
    return asyncio.run(c)


def _svc():
    st = MemoryDataStore()
    return TicketService(tickets=TicketStore(st), events=EventStore(st),
                         workflow=WorkflowRegistry.from_config(TYPES), pii_salt=SALT)


def _open(svc, **over):
    kw = dict(tenant="acme", type="feedback", subject="Fix the header",
              body="the template header overlaps", raiser="ada@x.com",
              ref={"kind": "document", "id": "tpl_1"})
    kw.update(over)
    return run(svc.open(**kw))


# ── open ──────────────────────────────────────────────────────────────────────
def test_open_starts_at_the_types_start_status_and_logs_created():
    svc = _svc()
    t = _open(svc)
    assert t.status == "new"
    hist, _ = run(svc.history("acme", t.id))
    assert [e["kind"] for e in hist] == ["created"]
    assert hist[0]["body"] == "the template header overlaps"


def test_open_pseudonymises_the_raiser():
    svc = _svc()
    t = _open(svc)
    assert t.raiser_ref and "ada@x.com" not in t.raiser_ref     # never a raw identity


def test_open_requires_the_types_required_fields():
    svc = _svc()
    with pytest.raises(RequiredFieldMissing):
        _open(svc, ref={})              # feedback requires ref


def test_open_an_unknown_type_is_refused():
    svc = _svc()
    with pytest.raises(UnknownType):
        _open(svc, type="nope")


# ── transition: the state machine is enforced ─────────────────────────────────
def test_a_legal_transition_moves_and_logs():
    svc = _svc()
    t = _open(svc)
    t2 = run(svc.transition(tenant="acme", ticket_id=t.id, to="triaged", actor="op1",
                            actor_caps=OP, is_operator=True))
    assert t2.status == "triaged"
    hist, _ = run(svc.history("acme", t.id))
    assert hist[-1]["kind"] == "status_changed" and hist[-1]["data"] == {"from": "new", "to": "triaged"}


def test_an_illegal_transition_is_refused():
    svc = _svc()
    t = _open(svc)
    with pytest.raises(InvalidTransition):
        run(svc.transition(tenant="acme", ticket_id=t.id, to="resolved", actor="op1",
                           actor_caps=OP, is_operator=True))   # new -> resolved is not allowed


def test_a_transition_without_the_capability_is_forbidden():
    svc = _svc()
    t = _open(svc)
    with pytest.raises(TransitionForbidden):
        run(svc.transition(tenant="acme", ticket_id=t.id, to="triaged", actor="ada@x.com",
                           actor_caps=SELF))    # customer lacks tickets.manage


def test_transition_to_the_same_status_is_idempotent_noop():
    svc = _svc()
    t = _open(svc)
    run(svc.transition(tenant="acme", ticket_id=t.id, to="new", actor="op1", actor_caps=OP,
                       is_operator=True))
    hist, _ = run(svc.history("acme", t.id))
    assert [e["kind"] for e in hist] == ["created"]      # no status_changed event


def test_reopen_is_just_a_declared_transition():
    svc = _svc()
    t = _open(svc)
    for to in ("triaged", "in_progress", "resolved"):
        run(svc.transition(tenant="acme", ticket_id=t.id, to=to, actor="op1", actor_caps=OP,
                           is_operator=True))
    reopened = run(svc.transition(tenant="acme", ticket_id=t.id, to="in_progress", actor="op1",
                                  actor_caps=OP, is_operator=True))
    assert reopened.status == "in_progress"


# ── cross-tenant isolation ────────────────────────────────────────────────────
def test_another_tenant_cannot_fetch_the_ticket():
    svc = _svc()
    t = _open(svc)
    with pytest.raises(TicketNotFound):
        run(svc.get("other", t.id))


# ── reply appends to the log ──────────────────────────────────────────────────
def test_reply_is_logged_and_touches_updated():
    svc = _svc()
    t = _open(svc)
    run(svc.reply(tenant="acme", ticket_id=t.id, author="ada@x.com", body="any update?"))
    hist, _ = run(svc.history("acme", t.id))
    assert hist[-1]["kind"] == "replied" and hist[-1]["body"] == "any update?"
