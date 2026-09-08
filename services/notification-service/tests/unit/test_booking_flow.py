"""The config-driven booking engine: a full multi-step flow, BACK, atomic reserve, session isolation.

Providers are fakes — the engine owns orchestration only; who is free when is the provider's domain.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.booking_flow import (BookingEngine, BookingError, FlowDefinition, SessionStore,
                                   CALENDAR, CONFIRM, SELECT, SUCCESS, TIME)


def run(c):
    return asyncio.run(c)


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class _Avail:
    async def options(self, *, tenant, dimension, chosen):
        if dimension == "service":
            return [{"id": "cut", "title": "Haircut"}, {"id": "color", "title": "Colour"}]
        if dimension == "staff":
            # staff depends on the chosen service — proves chosen flows to the provider
            return [{"id": f"dr_{chosen.get('service')}", "title": "Dr. A"}]
        return []

    async def calendar(self, *, tenant, chosen):
        # the calendar reflects the chosen staff — each resource its own schedule
        return {"min_date": "2026-09-08", "max_date": "2026-10-08",
                "unavailable_dates": ["2026-09-10"], "include_days": ["Sun", "Mon", "Tue"],
                "_staff": chosen.get("staff")}

    async def slots(self, *, tenant, chosen, date):
        return [{"id": f"{date}T10:00", "title": "10:00"}, {"id": f"{date}T11:00", "title": "11:00"}]


class _Booking:
    def __init__(self, ok=True, reason=""):
        self.ok, self.reason, self.calls = ok, reason, []

    async def reserve(self, *, tenant, selection, idempotency_key):
        self.calls.append({"selection": dict(selection), "key": idempotency_key})
        return {"ok": self.ok, "ref": "BK123", "reason": self.reason}


class _Defs:
    def __init__(self, defn):
        self.defn = defn

    async def get(self, tenant):
        return self.defn


def _defn():
    return FlowDefinition(steps=(
        {"dimension": "service"}, {"dimension": "staff"},
        {"dimension": "date"}, {"dimension": "time"}, {"dimension": "confirm"}),
        strings={"pick_staff": "اختر الطبيب"})


def _engine(booking=None, defn=None):
    st = MemoryDataStore()
    return BookingEngine(definitions=_Defs(defn or _defn()), availability=_Avail(),
                         booking=booking or _Booking(),
                         sessions=SessionStore(st, clock=_Clock()))


TOK = "flow_tok_1"


def _init(e, tenant="t1"):
    return run(e.handle({"action": "INIT", "flow_token": TOK}, tenant=tenant))


def _submit(e, screen, data, tenant="t1"):
    return run(e.handle({"action": "data_exchange", "screen": screen, "flow_token": TOK,
                         "data": data}, tenant=tenant))


# ── the full happy path ──────────────────────────────────────────────────────
def test_full_booking_walks_every_step_to_success():
    bk = _Booking()
    e = _engine(bk)
    r = _init(e)
    assert r["screen"] == SELECT and r["data"]["dimension"] == "service"
    r = _submit(e, SELECT, {"selection": "cut"})
    assert r["screen"] == SELECT and r["data"]["dimension"] == "staff"
    assert r["data"]["title"] == "اختر الطبيب"                 # i18n from strings
    assert r["data"]["options"][0]["id"] == "dr_cut"           # staff depended on the service
    r = _submit(e, SELECT, {"selection": "dr_cut"})
    assert r["screen"] == CALENDAR
    assert r["data"]["unavailable_dates"] == ["2026-09-10"] and r["data"]["include_days"] == ["Sun", "Mon", "Tue"]
    r = _submit(e, CALENDAR, {"date": "2026-09-09"})
    assert r["screen"] == TIME and r["data"]["date"] == "2026-09-09"
    assert r["data"]["slots"][0]["id"] == "2026-09-09T10:00"
    r = _submit(e, TIME, {"time": "2026-09-09T10:00"})
    assert r["screen"] == CONFIRM and r["data"]["summary"]["service"] == "cut"
    r = _submit(e, CONFIRM, {})
    assert r["screen"] == SUCCESS
    assert r["data"]["extension_message_response"]["params"]["booking_ref"] == "BK123"
    # the provider saw the complete selection, keyed idempotently by the flow token
    assert bk.calls[0]["selection"] == {"service": "cut", "staff": "dr_cut",
                                        "date": "2026-09-09", "time": "2026-09-09T10:00"}
    assert bk.calls[0]["key"] == TOK


def test_a_single_chair_shop_skips_staff():
    e = _engine(defn=FlowDefinition(steps=(
        {"dimension": "service"}, {"dimension": "date"}, {"dimension": "time"},
        {"dimension": "confirm"})))
    _init(e)
    r = _submit(e, SELECT, {"selection": "cut"})
    assert r["screen"] == CALENDAR                              # no staff step


# ── BACK ─────────────────────────────────────────────────────────────────────
def test_back_returns_to_the_prior_step_and_clears_its_choice():
    e = _engine()
    _init(e)
    _submit(e, SELECT, {"selection": "cut"})
    r = _submit(e, SELECT, {"selection": "dr_cut"})
    assert r["screen"] == CALENDAR
    back = run(e.handle({"action": "BACK", "flow_token": TOK}, tenant="t1"))
    assert back["screen"] == SELECT and back["data"]["dimension"] == "staff"   # back to staff pick
    # re-picking works and proceeds
    r = _submit(e, SELECT, {"selection": "dr_cut"})
    assert r["screen"] == CALENDAR


# ── reserve failure (double-book / just-taken) ───────────────────────────────
def test_a_failed_reserve_returns_to_confirm_with_an_error_not_success():
    bk = _Booking(ok=False, reason="Slot just taken")
    e = _engine(bk)
    _init(e)
    _submit(e, SELECT, {"selection": "cut"})
    _submit(e, SELECT, {"selection": "dr_cut"})
    _submit(e, CALENDAR, {"date": "2026-09-09"})
    _submit(e, TIME, {"time": "2026-09-09T10:00"})
    r = _submit(e, CONFIRM, {})
    assert r["screen"] == CONFIRM and r["data"]["error_message"] == "Slot just taken"


def test_an_empty_selection_is_refused_with_an_error():
    e = _engine()
    _init(e)
    r = _submit(e, SELECT, {})                                  # nothing picked
    assert r["screen"] == SELECT and "error_message" in r["data"]


# ── session isolation ────────────────────────────────────────────────────────
def test_sessions_are_tenant_scoped():
    e = _engine()
    _init(e, tenant="shop_a")
    _submit(e, SELECT, {"service": "cut"}, tenant="shop_a")
    # a different tenant with the SAME flow_token starts fresh, never sees shop_a's progress
    r = run(e.handle({"action": "data_exchange", "screen": SELECT, "flow_token": TOK,
                      "data": {"selection": "color"}}, tenant="shop_b"))
    # shop_b had no session → step 0 recorded service, advances to staff (not shop_a's calendar)
    assert r["screen"] == SELECT and r["data"]["dimension"] == "staff"


# ── definition validation ────────────────────────────────────────────────────
def test_a_definition_without_date_or_confirm_is_refused():
    with pytest.raises(BookingError):
        FlowDefinition(steps=({"dimension": "service"},))
    with pytest.raises(BookingError):
        FlowDefinition(steps=({"dimension": "date"},))          # no confirm
    with pytest.raises(BookingError):
        FlowDefinition(steps=({"dimension": "confirm"}, {"dimension": "date"}))  # confirm not last
