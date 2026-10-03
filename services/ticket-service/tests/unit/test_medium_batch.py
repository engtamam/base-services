"""Non-security MEDIUM remediation (ticket-service):
  008  NotifierOutbox dead-letters a row after NOTIFY_MAX_ATTEMPTS and surfaces an unexpected bug.
  021  rate() is once-only; a second rating is refused (AlreadyRated), the first is the KPI.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import FAILED, NotifierOutbox
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import AlreadyRated, TicketService
from app.core.workflow import WorkflowRegistry
from app.ports.notification_sender import NotificationUnavailable

SALT = "s" * 32
_TYPES = [{"key": "support", "label": {"en": "S"}, "statuses": ["new", "open", "closed"],
           "start": "new", "transitions": {"new": ["open", "closed"], "open": ["closed"]},
           "terminal": ["closed"], "required_fields": [],
           "transition_capabilities": {"open": "tickets.manage", "closed": "tickets.manage"}}]


def run(c):
    return asyncio.run(c)


def _svc():
    st = MemoryDataStore()
    return st, TicketService(tickets=TicketStore(st), events=EventStore(st),
                             workflow=WorkflowRegistry.from_config(_TYPES), pii_salt=SALT)


# ── 021: once-only rating ─────────────────────────────────────────────────────
def test_rate_is_once_only():
    st, svc = _svc()
    t = run(svc.open(tenant="acme", type="support", subject="hi", body="b", raiser="u1"))
    run(svc.rate(tenant="acme", ticket_id=t.id, rater="u1", score=5))
    with pytest.raises(AlreadyRated):
        run(svc.rate(tenant="acme", ticket_id=t.id, rater="u1", score=1))
    # the FIRST rating stands
    row = run(st.get("tickets", t.id))
    assert row["meta"]["rating"]["score"] == 5


# ── 008: notifier max-attempts dead-letter + bug surfacing ────────────────────
class _Telemetry:
    def __init__(self): self.events = []
    def event(self, name, data): self.events.append((name, data))


class _DownSender:
    async def send(self, **k):
        raise NotificationUnavailable("down")


class _BuggySender:
    async def send(self, **k):
        raise KeyError("code bug")


def _row(attempts=0):
    return {"id": "n1", "tenant": "t", "ticket_id": "tk", "template": "x", "event": "created",
            "subject_ref": "r", "to": "operators", "data": {}, "source": "s",
            "status": "pending", "attempts": attempts, "next_attempt_at": 0.0, "last_error": ""}


def test_notifier_dead_letters_after_max_attempts():
    tel = _Telemetry()
    st = MemoryDataStore()
    ob = NotifierOutbox(st, _DownSender(), cfg={"notify_max_attempts": 3}, clock=lambda: 0.0,
                        telemetry=tel)
    row = _row(attempts=2)              # the next failure is the 3rd → dead-letter
    run(st.put("notifications", row["id"], row))
    run(ob.deliver(row))
    assert row["status"] == FAILED
    assert any(n == "notify.dead_lettered" for n, _ in tel.events)


def test_notifier_dead_letters_an_unexpected_bug():
    tel = _Telemetry()
    st = MemoryDataStore()
    ob = NotifierOutbox(st, _BuggySender(), cfg={"notify_max_attempts": 12}, clock=lambda: 0.0,
                        telemetry=tel)
    row = _row()
    run(st.put("notifications", row["id"], row))
    run(ob.deliver(row))               # must not retry a code bug as transport forever
    assert row["status"] == FAILED
    assert any(n == "notify.delivery_error" for n, _ in tel.events)


def test_notifier_retries_a_transient_failure_below_the_ceiling():
    st = MemoryDataStore()
    ob = NotifierOutbox(st, _DownSender(), cfg={"notify_max_attempts": 12, "retry_base_seconds": 1,
                                                "retry_max_seconds": 10}, clock=lambda: 100.0)
    row = _row()
    run(st.put("notifications", row["id"], row))
    run(ob.deliver(row))
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_attempt_at"] > 100.0
