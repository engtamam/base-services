"""TicketErasure — redact the subject's free text, keep the pseudonymous record (53.6)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import EventStore, TicketStore
from app.core.erasure import TicketErasure
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry

SALT = "s" * 32
TYPES = [{"key": "feedback", "label": {"en": "F"}, "statuses": ["new", "closed"], "start": "new",
          "transitions": {"new": ["closed"]}, "terminal": ["closed"], "required_fields": []}]


def run(c):
    return asyncio.run(c)


def _world():
    st = MemoryDataStore()
    svc = TicketService(tickets=TicketStore(st), events=EventStore(st),
                        workflow=WorkflowRegistry.from_config(TYPES), pii_salt=SALT)
    return st, svc


def test_erasure_redacts_free_text_but_keeps_the_record():
    st, svc = _world()
    t = run(svc.open(tenant="acme", type="feedback", subject="my email is ada@x.com",
                     body="please call me on 0555", raiser="ada@x.com"))
    run(svc.reply(tenant="acme", ticket_id=t.id, author="ada@x.com", body="still waiting"))

    out = run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "ada@x.com"))
    assert out["already_absent"] is False and out["subject_ref"] == t.raiser_ref

    reloaded = run(TicketStore(st).fetch(t.id))
    assert reloaded is not None                      # the record survives
    assert reloaded.subject == "[erased]"            # free text gone
    assert reloaded.status == "new" and reloaded.type == "feedback"   # shape kept
    hist, _ = run(svc.history("acme", t.id))
    assert all(e["body"] in ("", "[erased]") for e in hist)   # authored bodies redacted


def test_erasure_is_idempotent():
    st, svc = _world()
    t = run(svc.open(tenant="acme", type="feedback", subject="hi", body="hello", raiser="ada@x.com"))
    run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "ada@x.com"))
    again = run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "ada@x.com"))
    assert again["already_absent"] is False          # the ticket is still there (redacted)
    assert run(TicketStore(st).fetch(t.id)).subject == "[erased]"


def test_erasing_an_absent_subject_says_so():
    st, _ = _world()
    out = run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "nobody@x.com"))
    assert out["already_absent"] is True and out["erased"] == []


def test_another_subjects_ticket_is_untouched():
    st, svc = _world()
    keep = run(svc.open(tenant="acme", type="feedback", subject="grace here", body="hi",
                        raiser="grace@x.com"))
    run(svc.open(tenant="acme", type="feedback", subject="ada here", body="hi", raiser="ada@x.com"))
    run(TicketErasure(store=st, pii_salt=SALT).erase("acme", "ada@x.com"))
    assert run(TicketStore(st).fetch(keep.id)).subject == "grace here"   # grace untouched
