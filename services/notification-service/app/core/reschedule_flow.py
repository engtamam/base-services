"""Flow #1 — RESCHEDULE / CANCEL an existing appointment. A dynamic-Flow handler on the generic
ladder (L1-L3), reusing the booking screens (SELECT/CALENDAR/TIME/CONFIRM/SUCCESS) and SessionStore.

The customer never types their booking ref: the sender puts the customer id in the Flow's seed data
(build_flow(flow_action="data_exchange", data={"customer": "<id>"})), the endpoint's INIT carries it,
and we load THAT customer's upcoming appointments. The platform owns the domain behind one port; we
own the state machine + crypto + sessions. reschedule/cancel MUST be atomic+idempotent on the
flow_token, exactly like a new booking (no double-move, no double-cancel)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.booking_flow import (CALENDAR, CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, TIME,
                                   ProviderUnavailable, SessionStore, _submitted_field)

INIT, BACK, DATA_EXCHANGE = "INIT", "BACK", "data_exchange"
_RESCHEDULE, _CANCEL = "reschedule", "cancel"


@runtime_checkable
class ReschedulingProvider(Protocol):
    """Implement over your OWN live bookings. `customer` identifies whose appointments to load (from
    the Flow's seed data). Raise / non-2xx on failure → retry snackbar, never an empty list or a fake
    action. cancel/reschedule MUST be atomic + idempotent on `idempotency_key` (the flow_token)."""

    async def appointments(self, *, tenant: str, customer: str) -> list[dict[str, Any]]:
        """The customer's changeable upcoming appointments. -> [{"id","title"}] (title e.g.
        "Dr. Ali — Tue 10:00"). Return only ones still reschedulable/cancellable."""
        ...

    async def calendar(self, *, tenant: str, appointment: str) -> dict[str, Any]:
        """Date constraints for rescheduling THIS appointment (same resource). ->
        {"min_date","max_date","unavailable_dates":[...],"include_days":[...]}."""
        ...

    async def slots(self, *, tenant: str, appointment: str, date: str) -> list[dict[str, Any]]:
        """Free times on `date` for rescheduling this appointment. -> [{"id","title"}]."""
        ...

    async def cancel(self, *, tenant: str, appointment: str,
                     idempotency_key: str) -> dict[str, Any]:
        """Cancel it. -> {"ok":true,"ref"?} | {"ok":false,"reason"?}."""
        ...

    async def reschedule(self, *, tenant: str, appointment: str, slot: str,
                         idempotency_key: str) -> dict[str, Any]:
        """Move it to `slot`. -> {"ok":true,"ref"?} | {"ok":false,"reason"?}. Atomic on the new slot."""
        ...


# Session steps of the fixed (branching) machine.
_LIST, _ACTION, _CONFIRM_CANCEL, _DATE, _TIME, _CONFIRM_MOVE, _DONE = (
    "list", "action", "confirm_cancel", "date", "time", "confirm_move", "done")


class RescheduleEngine:
    """FlowHandler (name="reschedule"). Register in the FlowRegistry alongside booking."""

    name = "reschedule"

    def __init__(self, *, provider: ReschedulingProvider, sessions: SessionStore,
                 strings: dict[str, str] | None = None) -> None:
        self._p = provider
        self._sessions = sessions
        self._s = strings or {}

    def _label(self, key: str, default: str) -> str:
        return self._s.get(key, default)

    async def handle(self, payload: dict[str, Any], *, tenant: str = "") -> dict[str, Any]:
        token = str(payload.get("flow_token") or "")
        data = payload.get("data") or {}
        action = payload.get("action")
        try:
            return await self._dispatch(tenant, token, action, data)
        except ProviderUnavailable:
            return {"screen": payload.get("screen") or CONFIRM,
                    "data": {"error_message": self._label(
                        "backend_error", "Sorry, we could not load your appointments. Try again.")}}

    async def _dispatch(self, tenant: str, token: str, action: str,
                        data: dict[str, Any]) -> dict[str, Any]:
        if action == INIT:
            st = {"customer": str(data.get("customer") or ""), "step": _LIST, "appt": "", "date": ""}
            await self._sessions.save(tenant, token, st)
            return await self._list_screen(tenant, st)

        st = await self._sessions.load(tenant, token) or {"customer": "", "step": _LIST}
        step = st.get("step")

        if action == BACK:
            return await self._list_screen(tenant, st)       # a change flow is short; back = restart list

        if step == _LIST:
            st["appt"] = data.get(SELECTION_FIELD, "")
            if not st["appt"]:
                return await self._list_screen(tenant, st, error=self._label("required", "Please choose."))
            st["step"] = _ACTION
            await self._sessions.save(tenant, token, st)
            return self._action_screen()

        if step == _ACTION:
            choice = data.get(SELECTION_FIELD)
            if choice == _CANCEL:
                st["step"] = _CONFIRM_CANCEL
                await self._sessions.save(tenant, token, st)
                return self._confirm_screen(self._label("confirm_cancel", "Cancel this appointment?"))
            if choice == _RESCHEDULE:
                st["step"] = _DATE
                await self._sessions.save(tenant, token, st)
                return await self._calendar_screen(tenant, st)
            return self._action_screen(error=self._label("required", "Please choose."))

        if step == _CONFIRM_CANCEL:
            res = await self._p.cancel(tenant=tenant, appointment=st["appt"], idempotency_key=token)
            if not res.get("ok"):
                return self._confirm_screen(self._label("confirm_cancel", "Cancel this appointment?"),
                                            error=res.get("reason") or "Could not cancel. Try again.")
            await self._sessions.clear(tenant, token)
            return self._success(token, res.get("ref", ""))

        if step == _DATE:
            st["date"] = data.get(_submitted_field("date"), "")
            if not st["date"]:
                return await self._calendar_screen(tenant, st, error=self._label("required", "Pick a date."))
            st["step"] = _TIME
            await self._sessions.save(tenant, token, st)
            return await self._time_screen(tenant, st)

        if step == _TIME:
            st["slot"] = data.get(_submitted_field("time"), "")
            if not st["slot"]:
                return await self._time_screen(tenant, st, error=self._label("required", "Pick a time."))
            st["step"] = _CONFIRM_MOVE
            await self._sessions.save(tenant, token, st)
            return self._confirm_screen(self._label("confirm_move", "Confirm the new time?"))

        if step == _CONFIRM_MOVE:
            res = await self._p.reschedule(tenant=tenant, appointment=st["appt"],
                                           slot=st.get("slot", ""), idempotency_key=token)
            if not res.get("ok"):
                return self._confirm_screen(self._label("confirm_move", "Confirm the new time?"),
                                            error=res.get("reason") or "That slot was just taken.")
            await self._sessions.clear(tenant, token)
            return self._success(token, res.get("ref", ""))

        return self._confirm_screen(self._label("confirm", "Confirm"),
                                    error="This session has ended. Please start again.")

    # ── screens (reuse the booking screen ids so ONE Flow JSON serves both) ──────
    async def _list_screen(self, tenant, st, *, error="") -> dict[str, Any]:
        opts = await self._p.appointments(tenant=tenant, customer=st.get("customer", ""))
        return {"screen": SELECT, "data": {"dimension": "appointment",
                "title": self._label("pick_appointment", "Your appointments"), "options": opts,
                **({"error_message": error} if error else {})}}

    def _action_screen(self, *, error="") -> dict[str, Any]:
        return {"screen": SELECT, "data": {"dimension": "action",
                "title": self._label("pick_action", "What would you like to do?"),
                "options": [{"id": _RESCHEDULE, "title": self._label("reschedule", "Reschedule")},
                            {"id": _CANCEL, "title": self._label("cancel", "Cancel")}],
                **({"error_message": error} if error else {})}}

    async def _calendar_screen(self, tenant, st, *, error="") -> dict[str, Any]:
        cal = await self._p.calendar(tenant=tenant, appointment=st["appt"])
        return {"screen": CALENDAR, "data": {"mode": "single",
                "title": self._label("pick_date", "Choose a new date"),
                "min_date": cal.get("min_date", ""), "max_date": cal.get("max_date", ""),
                "unavailable_dates": cal.get("unavailable_dates", []),
                "include_days": cal.get("include_days", []),
                **({"error_message": error} if error else {})}}

    async def _time_screen(self, tenant, st, *, error="") -> dict[str, Any]:
        slots = await self._p.slots(tenant=tenant, appointment=st["appt"], date=st.get("date", ""))
        return {"screen": TIME, "data": {"date": st.get("date", ""),
                "title": self._label("pick_time", "Choose a new time"), "slots": slots,
                **({"error_message": error} if error else {})}}

    def _confirm_screen(self, title, *, error="") -> dict[str, Any]:
        return {"screen": CONFIRM, "data": {"title": title,
                **({"error_message": error} if error else {})}}

    def _success(self, token, ref) -> dict[str, Any]:
        return {"screen": SUCCESS, "data": {"extension_message_response": {"params": {
            "flow_token": token, "booking_ref": ref}}}}
