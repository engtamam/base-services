"""Flow #1 — reschedule / cancel: both branches to SUCCESS, atomic-fail, provider outage."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.booking_flow import CALENDAR, CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, TIME, SessionStore
from app.core.reschedule_flow import ProviderUnavailable, RescheduleEngine


def run(c):
    return asyncio.run(c)


class _Prov:
    def __init__(self, ok=True, reason="", outage=False):
        self.ok, self.reason, self.outage, self.calls = ok, reason, outage, []

    async def appointments(self, *, tenant, customer):
        if self.outage:
            raise ProviderUnavailable("down")
        return [{"id": "ap1", "title": "Dr. Ali — Tue 10:00"}]

    async def calendar(self, *, tenant, appointment):
        return {"min_date": "2026-09-08", "max_date": "2026-10-08", "unavailable_dates": []}

    async def slots(self, *, tenant, appointment, date):
        return [{"id": f"{date}T11:00", "title": "11:00"}]

    async def cancel(self, *, tenant, appointment, idempotency_key):
        self.calls.append(("cancel", appointment, idempotency_key))
        return {"ok": self.ok, "ref": "CX1", "reason": self.reason}

    async def reschedule(self, *, tenant, appointment, slot, idempotency_key):
        self.calls.append(("reschedule", appointment, slot, idempotency_key))
        return {"ok": self.ok, "ref": "RB1", "reason": self.reason}


def _eng(prov=None):
    return RescheduleEngine(provider=prov or _Prov(),
                            sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0))


TOK = "tk"


def _init(e):
    return run(e.handle({"action": "INIT", "flow_token": TOK, "data": {"customer": "c9"}}, tenant="t1"))


def _sub(e, screen, data):
    return run(e.handle({"action": "data_exchange", "screen": screen, "flow_token": TOK, "data": data},
                        tenant="t1"))


def test_cancel_path_to_success():
    p = _Prov()
    e = _eng(p)
    r = _init(e)
    assert r["screen"] == SELECT and r["data"]["options"][0]["id"] == "ap1"
    r = _sub(e, SELECT, {SELECTION_FIELD: "ap1"})            # pick appointment
    assert r["screen"] == SELECT and {o["id"] for o in r["data"]["options"]} == {"reschedule", "cancel"}
    r = _sub(e, SELECT, {SELECTION_FIELD: "cancel"})         # choose cancel
    assert r["screen"] == CONFIRM
    r = _sub(e, CONFIRM, {})                                 # confirm
    assert r["screen"] == SUCCESS
    assert p.calls[-1] == ("cancel", "ap1", TOK)            # idempotent on the flow_token


def test_reschedule_path_to_success():
    p = _Prov()
    e = _eng(p)
    _init(e)
    _sub(e, SELECT, {SELECTION_FIELD: "ap1"})
    r = _sub(e, SELECT, {SELECTION_FIELD: "reschedule"})
    assert r["screen"] == CALENDAR
    r = _sub(e, CALENDAR, {"date": "2026-09-09"})
    assert r["screen"] == TIME and r["data"]["slots"][0]["id"] == "2026-09-09T11:00"
    r = _sub(e, TIME, {"time": "2026-09-09T11:00"})
    assert r["screen"] == CONFIRM
    r = _sub(e, CONFIRM, {})
    assert r["screen"] == SUCCESS
    assert p.calls[-1] == ("reschedule", "ap1", "2026-09-09T11:00", TOK)


def test_a_failed_cancel_stays_on_confirm_with_error():
    e = _eng(_Prov(ok=False, reason="Too late to cancel"))
    _init(e)
    _sub(e, SELECT, {SELECTION_FIELD: "ap1"})
    _sub(e, SELECT, {SELECTION_FIELD: "cancel"})
    r = _sub(e, CONFIRM, {})
    assert r["screen"] == CONFIRM and r["data"]["error_message"] == "Too late to cancel"


def test_provider_outage_is_a_snackbar():
    e = _eng(_Prov(outage=True))
    r = _init(e)
    assert "error_message" in r["data"]                      # never an empty appointment list
