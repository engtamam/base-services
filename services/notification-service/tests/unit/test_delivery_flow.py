"""Flow #2 — delivery slots: the SAME BookingEngine, just name='delivery' + a delivery definition.
Proves the slot engine is generic (delivery = capacity-limited windows) with zero new engine code."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.booking_flow import (CALENDAR, CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, TIME,
                                   BookingEngine, FlowDefinition, SessionStore)


def run(c):
    return asyncio.run(c)


class _Avail:
    async def options(self, *, tenant, dimension, chosen):
        if dimension == "zone":
            return [{"id": "north", "title": "North district"}]
        return []

    async def calendar(self, *, tenant, chosen):
        # delivery days depend on the chosen zone
        assert chosen.get("zone") == "north"
        return {"min_date": "2026-09-08", "max_date": "2026-09-15",
                "unavailable_dates": [], "include_days": ["Sun", "Mon", "Tue", "Wed", "Thu"]}

    async def slots(self, *, tenant, chosen, date):
        # capacity-limited windows, remaining count in the title
        return [{"id": f"{date}|10-12", "title": "10:00–12:00 (3 left)"},
                {"id": f"{date}|12-14", "title": "12:00–14:00 (1 left)"}]


class _Booking:
    def __init__(self):
        self.calls = []

    async def reserve(self, *, tenant, selection, idempotency_key):
        self.calls.append((dict(selection), idempotency_key))
        return {"ok": True, "ref": "DLV-1"}


class _Defs:
    async def get(self, tenant):
        return FlowDefinition(steps=({"dimension": "zone"}, {"dimension": "date"},
                                     {"dimension": "time"}, {"dimension": "confirm"}))


def _eng():
    bk = _Booking()
    e = BookingEngine(name="delivery", definitions=_Defs(), availability=_Avail(), booking=bk,
                      sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0))
    return e, bk


TOK = "tk"


def _init(e):
    return run(e.handle({"action": "INIT", "flow_token": TOK}, tenant="t1"))


def _sub(e, screen, data):
    return run(e.handle({"action": "data_exchange", "screen": screen, "flow_token": TOK, "data": data}, tenant="t1"))


def test_the_engine_reports_the_delivery_name():
    e, _ = _eng()
    assert e.name == "delivery"


def test_full_delivery_slot_booking():
    e, bk = _eng()
    r = _init(e)
    assert r["screen"] == SELECT and r["data"]["options"][0]["id"] == "north"
    r = _sub(e, SELECT, {SELECTION_FIELD: "north"})
    assert r["screen"] == CALENDAR and r["data"]["include_days"] == ["Sun", "Mon", "Tue", "Wed", "Thu"]
    r = _sub(e, CALENDAR, {"date": "2026-09-09"})
    assert r["screen"] == TIME and "10:00–12:00 (3 left)" in [s["title"] for s in r["data"]["slots"]]
    r = _sub(e, TIME, {"time": "2026-09-09|10-12"})
    assert r["screen"] == CONFIRM
    r = _sub(e, CONFIRM, {})
    assert r["screen"] == SUCCESS
    assert bk.calls[-1][0] == {"zone": "north", "date": "2026-09-09", "time": "2026-09-09|10-12"}
    assert bk.calls[-1][1] == TOK                            # idempotent on the flow_token
