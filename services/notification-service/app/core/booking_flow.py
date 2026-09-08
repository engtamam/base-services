"""Config-driven WhatsApp booking engine — the state machine behind the Flow data endpoint.

See docs/design/whatsapp-booking-flow.md. This owns the GENERIC orchestration; the platform owns its
domain behind two ports (AvailabilityProvider, BookingProvider) and a per-tenant FlowDefinition. The
engine never names "doctor", "salon", or any business rule: a "staff" step is just one selection
dimension, and "each doctor has their own schedule" falls out because the chosen staff id is passed
to the provider when the calendar/slots are computed.

Screens are a fixed, reusable set that a companion Flow JSON declares (provisioning generates it):
  SELECT   — one screen reused for every selection dimension (service/staff/location/party size),
             its list bound to ${data.options}; the submitted field name is the dimension.
  CALENDAR — the CalendarPicker/DatePicker, constraints from ${data.*}.
  TIME     — the slot list for the chosen date, bound to ${data.slots}.
  CONFIRM  — a summary + a data_exchange button.
  SUCCESS  — terminal; posts booking_ref back to chat.

The engine returns bare {screen, data} dicts; flow_endpoint.process encrypts them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

INIT, BACK, DATA_EXCHANGE = "INIT", "BACK", "data_exchange"
SELECT, CALENDAR, TIME, CONFIRM, SUCCESS = "SELECT", "CALENDAR", "TIME", "CONFIRM", "SUCCESS"

# The terminal spine — every dimension NOT in here is a "selection" dimension resolved via options().
_DATE, _TIME, _CONFIRM = "date", "time", "confirm"

# The CANONICAL submitted-field name per screen type — MUST match the generated Flow JSON's component
# `name` (a single reused SELECT screen has one fixed field, not the dimension). The date/time pickers
# carry their own name. Kept in one place so the engine and the Flow-JSON generator can never drift.
SELECTION_FIELD = "selection"


def _submitted_field(dimension: str) -> str:
    if dimension == _DATE:
        return _DATE
    if dimension == _TIME:
        return _TIME
    return SELECTION_FIELD


class BookingError(ValueError):
    """A definition or request the engine refuses before touching a provider."""


class ProviderUnavailable(Exception):
    """The platform's availability/booking backend errored or was unreachable. Surfaced to the
    customer as a snackbar on the same screen — never a silent empty calendar (looks like 'fully
    booked') or a fabricated booking."""


@runtime_checkable
class AvailabilityProvider(Protocol):
    """Implement these three (over HTTP or in-process) to feed the booking. `tenant` = the shop;
    `chosen` = everything picked so far (e.g. {"service":"cut","staff":"dr_7"}). SCOPE every answer to
    `chosen` — that is how each resource gets its OWN schedule (you're handed the chosen staff/branch).
    Raise (or return non-2xx over HTTP) on failure → the customer sees a retry snackbar, never an empty
    calendar or a fake booking. Keep it fast (WhatsApp expects ~10-15s end to end)."""

    async def options(self, *, tenant: str, dimension: str,
                      chosen: dict[str, Any]) -> list[dict[str, Any]]:
        """Choices for a selection step (dimension = "service" | "staff" | ... per your definition).
        Return only ones valid for `chosen`. -> [{"id","title","description"?}]. id comes back in chosen.

        CONDITIONAL / SKIPPABLE STEP: return an EMPTY list (or the sentinel [{"skip": true}]) to tell
        the engine this step does NOT apply to THIS booking — it advances past it silently instead of
        rendering an empty SELECT, and the dimension is simply absent from `chosen`. Use it when a prior
        choice makes a selection pointless (e.g. a service that doesn't need "who does it": you then
        auto-assign at reserve/calendar time). Same tenant, same flow, decided per booking from prior
        selections — no second FlowDefinition or engine instance needed. Returning real options renders
        the step as usual.

        LOYALTY-IN-BOOKING (optional): to let a customer spend points DURING a booking (e.g. a free 4th
        service), add a `reward` step to the definition; for dimension=="reward" return the rewards
        applicable to `chosen` from your loyalty program. The chosen reward flows to reserve(), which
        books + redeems atomically (see reserve's docstring). Leave the step out → no link. This is
        just a normal selection step — the engine stays loyalty-agnostic."""
        ...

    async def calendar(self, *, tenant: str, chosen: dict[str, Any]) -> dict[str, Any]:
        """Date constraints for the chosen resource. -> {"min_date","max_date","unavailable_dates":[...],
        "include_days":[...]}; dates "YYYY-MM-DD", include_days weekdays "Sun".."Sat". Omit what you
        don't constrain. unavailable_dates = fully-booked/closed days."""
        ...

    async def slots(self, *, tenant: str, chosen: dict[str, Any],
                    date: str) -> list[dict[str, Any]]:
        """Free times on `date` for the chosen resource. -> [{"id","title"}] (id opaque, echoed at
        reserve; title shown). Return only genuinely free slots. Not called in range mode."""
        ...


@runtime_checkable
class BookingProvider(Protocol):
    async def reserve(self, *, tenant: str, selection: dict[str, Any],
                      idempotency_key: str) -> dict[str, Any]:
        """Commit the booking. `selection` = the full chosen set (incl. date/time). MUST be ATOMIC +
        IDEMPOTENT on `idempotency_key` (we pass the WhatsApp flow_token): a redelivered confirm and
        two customers racing for the last slot must never double-book — first wins, rest get ok:false.
        -> {"ok":true,"ref":"BK-123"} (ref shown in chat) | {"ok":false,"reason":"just taken"}.

        LOYALTY LINK (optional, on/off by config — no engine coupling): add a `reward` selection step
        to the flow definition whose options() returns the rewards applicable to `chosen` (e.g. a free
        4th service). The chosen reward then arrives here in `selection`; reserve books AND redeems the
        points in ONE atomic transaction on your side. Omit the step → no link. Standalone loyalty
        (the loyalty flow) is unaffected either way."""
        ...


@dataclass(frozen=True)
class FlowDefinition:
    """A tenant's booking flow: the ordered steps and the calendar knobs. Everything customer-visible
    is in `strings` (i18n). Validated at construction so a broken definition never reaches runtime."""
    steps: tuple[dict[str, Any], ...]          # e.g. ({"dimension":"service"},{"dimension":"date"},...)
    horizon_days: int = 30
    mode: str = "single"                        # single | range
    min_days: int = 1
    max_days: int = 14
    default_working_days: tuple[str, ...] = ()
    strings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.steps:
            raise BookingError("a booking flow needs at least one step")
        dims = [s.get("dimension") for s in self.steps]
        if _DATE not in dims:
            raise BookingError("a booking flow must include a 'date' step")
        if _CONFIRM not in dims:
            raise BookingError("a booking flow must include a 'confirm' step")
        if dims[-1] != _CONFIRM:
            raise BookingError("'confirm' must be the last step")
        if _TIME in dims and self.mode == "range":
            raise BookingError("range mode has no single time step")
        if dims.index(_DATE) < len([d for d in dims if d not in (_DATE, _TIME, _CONFIRM)]):
            # date must come after every selection dimension (so calendar sees the full selection)
            for d in dims[dims.index(_DATE):]:
                if d not in (_DATE, _TIME, _CONFIRM):
                    raise BookingError("selection steps must come before 'date'")
        if self.mode not in ("single", "range"):
            raise BookingError("mode must be single or range")

    def label(self, key: str, default: str) -> str:
        return self.strings.get(key, default)


@runtime_checkable
class DefinitionProvider(Protocol):
    async def get(self, tenant: str) -> FlowDefinition | None: ...


class SessionStore:
    """Server-side flow session, keyed by (tenant, flow_token), short-TTL. Holds the accumulated
    selections so the client never re-sends prior steps, and so one tenant's session can never be
    read by another (the key is length-prefixed with the tenant — see tenant-scoped-idempotency)."""

    _COLL = "flow_sessions"

    def __init__(self, store, *, clock, ttl_seconds: int = 1800) -> None:
        self._s = store
        self._clock = clock
        self._ttl = int(ttl_seconds)

    @staticmethod
    def _key(tenant: str, token: str) -> str:
        return f"{len(tenant)}:{tenant}|{token}"

    async def load(self, tenant: str, token: str) -> dict[str, Any] | None:
        row = await self._s.get(self._COLL, self._key(tenant, token))
        if not row or float(row.get("expires_at", 0)) <= self._clock():
            return None
        return row.get("state")

    async def save(self, tenant: str, token: str, state: dict[str, Any]) -> None:
        await self._s.put(self._COLL, self._key(tenant, token),
                          {"state": state, "expires_at": self._clock() + self._ttl})

    async def clear(self, tenant: str, token: str) -> None:
        await self._s.delete(self._COLL, self._key(tenant, token))


class BookingEngine:
    """L4 — the SLOT-RESERVATION dynamic-Flow handler (implements flow_dispatch.FlowHandler). It is
    generic: "reserve a capacity-limited slot after some selections" describes appointments, DELIVERY
    WINDOWS, table reservations, rentals — all the same engine, differing only by definition +
    provider. So `name` is per-instance: register one as "booking", another as "delivery", etc., each
    with its own definition and backend. Rides on the generic L1-L3 layers."""

    def __init__(self, *, definitions: DefinitionProvider, availability: AvailabilityProvider,
                 booking: BookingProvider, sessions: SessionStore, name: str = "booking") -> None:
        self.name = name
        self._defs = definitions
        self._avail = availability
        self._booking = booking
        self._sessions = sessions

    @staticmethod
    def _is_selection(dim: str) -> bool:
        return dim not in (_DATE, _TIME, _CONFIRM)

    @staticmethod
    def _is_skip(options: list[dict[str, Any]]) -> bool:
        """The provider says this selection step doesn't apply to this booking: an empty list, or the
        explicit sentinel [{"skip": true}]. The engine advances past the step (no render, no chosen
        entry); the provider auto-assigns later."""
        if not options:
            return True
        return len(options) == 1 and isinstance(options[0], dict) and bool(options[0].get("skip"))

    async def handle(self, payload: dict[str, Any], *, tenant: str = "") -> dict[str, Any]:
        action = payload.get("action")
        token = str(payload.get("flow_token") or "")
        data = payload.get("data") or {}
        defn = await self._defs.get(tenant)
        if defn is None:
            return {"screen": payload.get("screen") or CONFIRM,
                    "data": {"error_message": "Booking is not configured."}}

        try:
            return await self._dispatch(defn, tenant, token, action, data)
        except ProviderUnavailable:
            # The backend errored — keep the customer where they are with a clear, non-fabricated
            # message rather than an empty screen or a false booking.
            return {"screen": payload.get("screen") or CONFIRM,
                    "data": {"error_message": defn.label(
                        "backend_error", "Sorry, we could not load availability. Please try again.")}}

    async def _dispatch(self, defn: FlowDefinition, tenant: str, token: str, action: str,
                        data: dict[str, Any]) -> dict[str, Any]:
        if action == INIT:
            state = {"chosen": {}, "step": 0}
            await self._sessions.save(tenant, token, state)
            return await self._render(defn, tenant, token, state)

        state = await self._sessions.load(tenant, token) or {"chosen": {}, "step": 0}

        if action == BACK:
            state["step"] = max(0, int(state["step"]) - 1)
            # drop what was chosen at (and after) the step we returned to, so a re-pick is clean
            kept = {s["dimension"] for s in defn.steps[:state["step"]]}
            state["chosen"] = {k: v for k, v in state["chosen"].items() if k in kept}
            await self._sessions.save(tenant, token, state)
            return await self._render(defn, tenant, token, state)

        # data_exchange — record the current step's submission, then advance (or reserve).
        step_i = int(state["step"])
        if step_i >= len(defn.steps):
            return {"screen": CONFIRM, "data": {"error_message": "This booking has already finished."}}
        dim = defn.steps[step_i]["dimension"]

        if dim == _CONFIRM:
            result = await self._booking.reserve(
                tenant=tenant, selection=dict(state["chosen"]), idempotency_key=token)
            if not result.get("ok"):
                return await self._confirm_screen(
                    defn, state["chosen"],
                    error=result.get("reason") or "That time was just taken. Please pick another.")
            await self._sessions.clear(tenant, token)
            return {"screen": SUCCESS, "data": {"extension_message_response": {"params": {
                "flow_token": token, "booking_ref": result.get("ref", "")}}}}

        # a selection / date / time submission. The submitted field name is CANONICAL per screen type
        # (a reused SELECT screen has one fixed component name, not the dimension) — see _submitted().
        value = data.get(_submitted_field(dim))
        if value in (None, ""):
            return await self._render(defn, tenant, token, state,
                                      error=defn.label("required", "Please make a selection."))
        state["chosen"][dim] = value
        state["step"] = step_i + 1
        await self._sessions.save(tenant, token, state)
        return await self._render(defn, tenant, token, state)

    async def _render(self, defn: FlowDefinition, tenant: str, token: str, state: dict[str, Any],
                      *, error: str = "") -> dict[str, Any]:
        chosen = state["chosen"]
        # Advance past any selection step the provider says doesn't apply to THIS booking (empty/skip
        # options) — persisting each advance so BACK and the next submission stay consistent — until we
        # land on a step to render (a real selection, date, time) or reach confirm.
        while True:
            step_i = int(state["step"])
            if step_i >= len(defn.steps):
                return await self._confirm_screen(defn, chosen, error=error)
            dim = defn.steps[step_i]["dimension"]
            if not self._is_selection(dim):
                break
            options = await self._avail.options(tenant=tenant, dimension=dim, chosen=chosen)
            if not self._is_skip(options):
                return {"screen": SELECT, "data": {
                    "dimension": dim, "title": defn.label(f"pick_{dim}", f"Choose {dim}"),
                    "options": options, **({"error_message": error} if error else {})}}
            state["step"] = step_i + 1
            await self._sessions.save(tenant, token, state)

        if dim == _DATE:
            cal = await self._avail.calendar(tenant=tenant, chosen=chosen)
            return {"screen": CALENDAR, "data": {
                "mode": defn.mode, "title": defn.label("pick_date", "Choose a date"),
                "min_date": cal.get("min_date", ""), "max_date": cal.get("max_date", ""),
                "unavailable_dates": cal.get("unavailable_dates", []),
                "include_days": cal.get("include_days", list(defn.default_working_days)),
                **({"min_days": defn.min_days, "max_days": defn.max_days} if defn.mode == "range" else {}),
                **({"error_message": error} if error else {})}}
        if dim == _TIME:
            slots = await self._avail.slots(tenant=tenant, chosen=chosen, date=chosen.get(_DATE, ""))
            return {"screen": TIME, "data": {
                "date": chosen.get(_DATE, ""), "title": defn.label("pick_time", "Choose a time"),
                "slots": slots, **({"error_message": error} if error else {})}}
        return await self._confirm_screen(defn, chosen, error=error)

    async def _confirm_screen(self, defn: FlowDefinition, chosen: dict[str, Any],
                              *, error: str = "") -> dict[str, Any]:
        return {"screen": CONFIRM, "data": {
            "title": defn.label("confirm", "Confirm your booking"),
            "summary": {k: v for k, v in chosen.items()},
            **({"error_message": error} if error else {})}}
