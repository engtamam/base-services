"""Listing events and deliveries.

Events were stored and reachable only by an id somebody already held, so replaying a failed
delivery meant knowing the event id in advance — which is exactly what an operator investigating a
failure does not have. And the delivery listing discarded the cursor the datastore returned, on the
dead letter queue of all places, where the whole job is working through a backlog until it is
empty.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Delivery, Event
from app.core.stores import DeliveryStore, EventStore


def run(c):
    return asyncio.run(c)


def _events(st, tenant="t1", n=3, type_="user.created"):
    store = EventStore(st)
    for i in range(n):
        run(store.save(Event(id=f"ev_{tenant}_{type_}_{i}", tenant=tenant, type=type_,
                             payload={"i": i}, created=f"2026-07-{i + 1:02d}T00:00:00Z")))
    return store


def test_events_can_be_listed_at_all():
    st = MemoryDataStore()
    store = _events(st, n=3)
    rows, _ = run(store.list("t1"))
    assert len(rows) == 3


def test_events_are_scoped_to_the_tenant():
    st = MemoryDataStore()
    store = _events(st, tenant="t1", n=2)
    _events(st, tenant="t2", n=4)
    rows, _ = run(store.list("t1"))
    assert rows and all(r["tenant"] == "t1" for r in rows)


def test_events_can_be_filtered_by_type():
    st = MemoryDataStore()
    store = _events(st, n=2, type_="user.created")
    _events(st, n=3, type_="order.paid")
    rows, _ = run(store.list("t1", type_="order.paid"))
    assert rows and all(r["type"] == "order.paid" for r in rows)


def test_the_event_listing_is_resumable():
    st = MemoryDataStore()
    store = _events(st, n=5)
    first, cursor = run(store.list("t1", limit=2))
    assert len(first) == 2 and cursor, "no cursor: the second page is unreachable"
    second, _ = run(store.list("t1", limit=2, after=cursor))
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})


def test_paging_reaches_every_event_exactly_once():
    st = MemoryDataStore()
    store = _events(st, n=7)
    seen, cursor = [], None
    for _ in range(10):
        rows, cursor = run(store.list("t1", limit=3, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert len(seen) == 7 and len(set(seen)) == 7


# ── the dead letter queue ─────────────────────────────────────────────────────
def _dead(st, n=5):
    store = DeliveryStore(st)
    for i in range(n):
        run(store.save(Delivery(id=f"dl_{i}", tenant="t1", event_id=f"ev_{i}",
                                endpoint_id="ep_1", status="dead",
                                created=f"2026-07-{i + 1:02d}T00:00:00Z")))
    return store


def test_the_dead_letter_queue_is_pageable():
    """The cursor was discarded here, so past the limit the backlog was unreachable and nothing in
    the response said so."""
    st = MemoryDataStore()
    store = _dead(st, n=5)
    first, cursor = run(store.dead_letters("t1", 2))
    assert len(first) == 2 and cursor, "the DLQ cannot be worked through past the first page"
    second, _ = run(store.dead_letters("t1", 2, after=cursor))
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})


def test_the_last_page_of_the_queue_reports_no_cursor():
    st = MemoryDataStore()
    store = _dead(st, n=2)
    _, cursor = run(store.dead_letters("t1", 50))
    assert not cursor
