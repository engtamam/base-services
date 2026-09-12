"""Round-2 fixes (ticket-service). Maps to docs/audits/round2/ticket-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.inbound_bridge import InboundBridge, InboundMessage
from app.core.pii import is_subject_ref
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry

SALT = "s" * 32
_TYPES = [{"key": "support", "label": {"en": "S"},
           "statuses": ["new", "open", "closed"], "start": "new",
           "transitions": {"new": ["open", "closed"], "open": ["closed"]},
           "terminal": ["closed"], "required_fields": [],
           "transition_capabilities": {"open": "tickets.manage", "closed": "tickets.manage"}}]


def run(c):
    return asyncio.run(c)


class _Interleave(MemoryDataStore):
    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _svc(st=None):
    st = st or MemoryDataStore()
    return st, TicketService(tickets=TicketStore(st), events=EventStore(st),
                             workflow=WorkflowRegistry.from_config(_TYPES), pii_salt=SALT)


async def _events(st, ticket_id):
    hist, _ = await EventStore(st).history(tenant="acme", ticket_id=ticket_id, limit=100)
    return hist


# r2-02 — an OPERATOR's identity is pseudonymized into the immutable event log (not stored raw).
def test_operator_actor_is_pseudonymized():
    st, svc = _svc()
    t = run(svc.open(tenant="acme", type="support", subject="hi", body="b", raiser="cust"))
    run(svc.reply(tenant="acme", ticket_id=t.id, author="agent@shop.com", body="hello",
                  is_operator=True))
    run(svc.transition(tenant="acme", ticket_id=t.id, to="open", actor="agent@shop.com",
                       actor_caps={"tickets.manage"}, is_operator=True))
    for e in run(_events(st, t.id)):
        ref = e.get("actor_ref", "")
        assert "agent@shop.com" not in ref               # raw operator identity never stored
        if ref:
            assert is_subject_ref(ref)


# r2-03 — assign does not clobber a concurrent status transition (both persist).
def test_assign_and_transition_do_not_clobber():
    st, svc = _svc(_Interleave())
    t = run(svc.open(tenant="acme", type="support", subject="hi", body="b", raiser="u"))

    async def _race():
        await asyncio.gather(
            svc.transition(tenant="acme", ticket_id=t.id, to="open", actor="op",
                           actor_caps={"tickets.manage"}, is_operator=True),
            svc.assign(tenant="acme", ticket_id=t.id, assignee="agent-1", actor="op"),
        )
    run(_race())
    final = run(svc.get("acme", t.id))
    assert final.status == "open"                         # transition survived
    assert final.assignee == "agent-1"                    # assign survived (no clobber)


# r2-04 — two concurrent messages from the SAME customer open exactly one ticket.
def test_concurrent_thread_grouping_opens_one_ticket():
    st, svc = _svc(_Interleave())
    bridge = InboundBridge(service=svc, workflow=WorkflowRegistry.from_config(_TYPES),
                           store=st, ticket_type="support")
    m1 = InboundMessage(channel="whatsapp", tenant="acme", sender="966500",
                        external_id="wamid.a", text="hi", timestamp=1.0)
    m2 = InboundMessage(channel="whatsapp", tenant="acme", sender="966500",
                        external_id="wamid.b", text="again", timestamp=2.0)

    async def _race():
        return await asyncio.gather(bridge.ingest(m1), bridge.ingest(m2))
    outcomes = run(_race())
    assert sorted(outcomes) == ["appended", "created"]    # one opened, the other grouped in
    rows, _ = run(TicketStore(st).find(tenant="acme"))
    assert len(rows) == 1                                  # NOT two duplicate tickets


# r2-06 — the inbound idempotency key is tenant-scoped: same external_id across tenants both ingest.
def test_inbound_idempotency_is_tenant_scoped():
    st, svc = _svc()
    bridge = InboundBridge(service=svc, workflow=WorkflowRegistry.from_config(_TYPES),
                           store=st, ticket_type="support")
    a = run(bridge.ingest(InboundMessage(channel="whatsapp", tenant="acme", sender="s1",
                                         external_id="shared", text="hi", timestamp=1.0)))
    b = run(bridge.ingest(InboundMessage(channel="whatsapp", tenant="globex", sender="s2",
                                         external_id="shared", text="hi", timestamp=1.0)))
    assert a == "created" and b == "created"              # tenant B not dropped as a duplicate of A
