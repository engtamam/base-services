"""The generic inbound->ticket bridge: open, append-to-open, idempotent dedupe, new-after-terminal,
sender pseudonymised (never raw)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.inbound_bridge import InboundBridge, InboundMessage
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry

SALT = "s" * 32
TYPES = [{"key": "support", "label": {"en": "S"}, "statuses": ["new", "open", "closed"],
          "start": "new", "transitions": {"new": ["open", "closed"], "open": ["closed"]},
          "terminal": ["closed"], "required_fields": []}]


def run(c):
    return asyncio.run(c)


def _world():
    st = MemoryDataStore()
    svc = TicketService(tickets=TicketStore(st), events=EventStore(st),
                        workflow=WorkflowRegistry.from_config(TYPES), pii_salt=SALT)
    bridge = InboundBridge(service=svc, workflow=WorkflowRegistry.from_config(TYPES), store=st,
                           ticket_type="support")
    return st, svc, bridge


def _msg(external_id, sender="966500", text="hi", tenant="acme"):
    return InboundMessage(channel="whatsapp", tenant=tenant, sender=sender,
                          external_id=external_id, text=text, timestamp=1.0)


def _tickets(st):                       # find() returns dict rows, not Ticket objects
    return run(TicketStore(st).find(tenant="acme"))[0]


def test_first_message_opens_a_ticket_with_sender_pseudonymised():
    st, svc, bridge = _world()
    assert run(bridge.ingest(_msg("wamid.1", text="my number is 0555"))) == "created"
    rows = _tickets(st)
    assert len(rows) == 1
    t = rows[0]
    assert t["type"] == "support" and t["status"] == "new"
    assert t["ref"] == {"kind": "whatsapp", "id": "966500"}
    assert t["raiser_ref"] and t["raiser_ref"] != "966500"     # pseudonym, never the raw phone


def test_second_message_appends_to_the_open_ticket():
    st, svc, bridge = _world()
    run(bridge.ingest(_msg("wamid.1")))
    assert run(bridge.ingest(_msg("wamid.2", text="still there?"))) == "appended"
    rows = _tickets(st)
    assert len(rows) == 1                                      # same ticket, not a second
    hist, _ = run(svc.history("acme", rows[0]["id"]))
    assert any(e["kind"] == "replied" and e["body"] == "still there?" for e in hist)


def test_redelivered_message_is_ingested_once():
    st, svc, bridge = _world()
    run(bridge.ingest(_msg("wamid.1")))
    assert run(bridge.ingest(_msg("wamid.1"))) == "skipped"    # same wamid → no duplicate
    hist, _ = run(svc.history("acme", _tickets(st)[0]["id"]))
    assert sum(1 for e in hist if e["kind"] == "created") == 1


def test_a_closed_ticket_makes_the_next_message_open_a_fresh_one():
    st, svc, bridge = _world()
    run(bridge.ingest(_msg("wamid.1")))
    t1 = _tickets(st)[0]
    run(svc.transition(tenant="acme", ticket_id=t1["id"], to="closed", actor="op",
                       actor_caps={"tickets.manage"}, is_operator=True))
    assert run(bridge.ingest(_msg("wamid.2"))) == "created"    # terminal → new ticket
    assert len(_tickets(st)) == 2


def test_two_tenants_same_sender_never_share_a_ticket():
    st, svc, bridge = _world()
    run(bridge.ingest(_msg("wamid.a", tenant="acme")))
    run(bridge.ingest(_msg("wamid.b", tenant="other")))
    assert len(run(TicketStore(st).find(tenant="acme"))[0]) == 1
    assert len(run(TicketStore(st).find(tenant="other"))[0]) == 1


def test_unaddressable_message_is_skipped():
    st, svc, bridge = _world()
    assert run(bridge.ingest(_msg("", sender="966500"))) == "skipped"   # no external id
    assert run(bridge.ingest(_msg("wamid.x", sender=""))) == "skipped"  # no sender
    assert _tickets(st) == []
