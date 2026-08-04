"""The notification outbox — record first, drain to notification-service, retry (53.5)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.notification.null import NullNotificationSender
from app.core.notifier import DELIVERED, FAILED, PENDING, NotifierOutbox, TicketNotifier, _audience
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import TicketService
from app.core.workflow import WorkflowRegistry
from app.ports.notification_sender import NotificationRefused, NotificationUnavailable

SALT = "s" * 32
TYPES = [{"key": "feedback", "label": {"en": "F"},
          "statuses": ["new", "triaged", "closed"], "start": "new",
          "transitions": {"new": ["triaged", "closed"], "triaged": ["closed"]},
          "terminal": ["closed"], "required_fields": [],
          "transition_capabilities": {"triaged": "tickets.manage"},
          "notify": {"on": ["created", "replied", "status_changed"], "template_prefix": "ticket.feedback"}}]
CFG = {"retry_base_seconds": 30, "retry_max_seconds": 3600}


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Sender:
    driver = "fake"

    def __init__(self, fail=None):
        self.sent = []
        self._fail = fail

    async def send(self, **kw):
        if self._fail:
            raise self._fail
        self.sent.append(kw)
        return True

    async def health(self):
        return True


def _svc(store, sender):
    outbox = NotifierOutbox(store, sender, cfg=CFG, clock=_Clock())
    return TicketService(tickets=TicketStore(store), events=EventStore(store),
                         workflow=WorkflowRegistry.from_config(TYPES), pii_salt=SALT,
                         notifier=TicketNotifier(outbox)), outbox


# ── recording ─────────────────────────────────────────────────────────────────
def test_opening_a_ticket_records_a_notification():
    st = MemoryDataStore()
    svc, _ = _svc(st, _Sender())
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="hello", raiser="ada@x.com"))
    rows, _ = run(st.find("notifications", {"status": PENDING}, limit=10))
    assert len(rows) == 1
    assert rows[0]["template"] == "ticket.feedback.created" and rows[0]["to"] == "operators"


def test_the_service_never_sends_inline_only_records():
    """Opening a ticket must not call the sender directly — delivery is the drain's job."""
    st = MemoryDataStore()
    sender = _Sender()
    svc, _ = _svc(st, sender)
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    assert sender.sent == []            # nothing delivered synchronously


# ── audience ──────────────────────────────────────────────────────────────────
def test_audience_routing():
    assert _audience("created", {}) == "operators"
    assert _audience("status_changed", {}) == "raiser"
    assert _audience("replied", {"by": "operator"}) == "raiser"
    assert _audience("replied", {"by": "customer"}) == "operators"


# ── draining ──────────────────────────────────────────────────────────────────
def test_drain_delivers_pending_rows():
    st = MemoryDataStore()
    sender = _Sender()
    svc, outbox = _svc(st, sender)
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    out = run(outbox.drain(limit=10))
    assert out["delivered"] == 1 and len(sender.sent) == 1
    rows, _ = run(st.find("notifications", {"status": DELIVERED}, limit=10))
    assert len(rows) == 1


def test_a_transport_failure_keeps_it_pending_for_retry():
    st = MemoryDataStore()
    svc, outbox = _svc(st, _Sender(fail=NotificationUnavailable("down")))
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    run(outbox.drain(limit=10))
    rows, _ = run(st.find("notifications", {"status": PENDING}, limit=10))
    assert len(rows) == 1 and rows[0]["attempts"] == 1


def test_a_permanent_refusal_fails_the_row():
    st = MemoryDataStore()
    svc, outbox = _svc(st, _Sender(fail=NotificationRefused("bad template")))
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    run(outbox.drain(limit=10))
    rows, _ = run(st.find("notifications", {"status": FAILED}, limit=10))
    assert len(rows) == 1


def test_null_sender_records_but_delivers_nothing():
    st = MemoryDataStore()
    svc, outbox = _svc(st, NullNotificationSender())
    run(svc.open(tenant="acme", type="feedback", subject="hi", body="x", raiser="ada@x.com"))
    out = run(outbox.drain(limit=10))
    assert out["delivered"] == 0
    rows, _ = run(st.find("notifications", {"status": PENDING}, limit=10))
    assert len(rows) == 1            # still recorded, ready for when a transport is wired
