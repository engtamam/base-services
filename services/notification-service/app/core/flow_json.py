"""Flow JSON generator — a CONVENIENCE that emits the companion Flow JSON matching the booking
engine's screen contract, so a platform doesn't reverse-engineer it. The platform uploads/edits it
its own way (we never call Meta). See docs/design/whatsapp-booking-flow.md.

The screens + field names MUST match core/booking_flow (SELECTION_FIELD, screen names, the data keys
the engine returns) — they are generated from the same constants so they cannot drift. Targets Flow
JSON version 7.3 / data_api_version 3.0 (verified against Meta 2026-09-08): CalendarPicker requires
v6.1+ and Meta no longer publishes flows on <=5.0, so 7.3 is the current recommended schema; the
data endpoint API is a separate axis, still 3.0. Validate on upload, since Meta revises the schema and
component props per version. Override _VERSION if a platform pins a different (>=6.1) schema.

The engine drives navigation via data_exchange responses, so every non-terminal screen submits to the
endpoint; routing_model declares the transitions the engine uses.
"""
from __future__ import annotations

from typing import Any

from app.core.booking_flow import (CALENDAR, CONFIRM, DISPLAY, INPUT, INPUT_FIELD, SELECT,
                                   SELECT_MULTI, SELECTION_FIELD, SUCCESS, TIME, FlowDefinition)

_VERSION = "7.3"        # Flow JSON schema; must be >=6.1 for CalendarPicker (see module docstring)
_DATA_API_VERSION = "3.0"


def _footer(label: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Footer", "label": label,
            "on-click-action": {"name": "data_exchange", "payload": payload}}


def _select_screen(defn: FlowDefinition) -> dict[str, Any]:
    # ONE screen reused for every selection dimension; the endpoint fills title + options each time.
    return {
        "id": SELECT, "title": defn.label("select_title", "Choose"),
        "data": {"title": {"type": "string", "__example__": "Choose a service"},
                 "options": {"type": "array",
                             "__example__": [{"id": "cut", "title": "Haircut"}]},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            {"type": "RadioButtonsGroup", "name": SELECTION_FIELD, "label": "${data.title}",
             "required": True, "data-source": "${data.options}"},
            _footer(defn.label("continue", "Continue"),
                    {SELECTION_FIELD: "${form." + SELECTION_FIELD + "}"})]}}


def _select_multi_screen(defn: FlowDefinition) -> dict[str, Any]:
    # The multi-choice twin of SELECT: a CheckboxGroup submitting a LIST under the same field name.
    # Rendered for a step with "multi": true; the endpoint fills title + options each time, exactly
    # as for SELECT. `min-selected-items` 1 mirrors the engine's "at least one" requirement.
    return {
        "id": SELECT_MULTI, "title": defn.label("select_title", "Choose"),
        "data": {"title": {"type": "string", "__example__": "Choose services"},
                 "options": {"type": "array",
                             "__example__": [{"id": "cut", "title": "Haircut"}]},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            {"type": "CheckboxGroup", "name": SELECTION_FIELD, "label": "${data.title}",
             "required": True, "min-selected-items": 1, "data-source": "${data.options}"},
            _footer(defn.label("continue", "Continue"),
                    {SELECTION_FIELD: "${form." + SELECTION_FIELD + "}"})]}}


def _input_screen(defn: FlowDefinition) -> dict[str, Any]:
    # ONE reused free-TEXT screen (Meta TextInput) for every {"input":"text"} step; the endpoint fills
    # the title each time. Submits INPUT_FIELD — the name the engine reads. input-type "text" so a
    # leading + or 0 on a phone number is preserved.
    return {
        "id": INPUT, "title": defn.label("input_title", "Enter a value"),
        "data": {"title": {"type": "string", "__example__": "Enter the customer's phone"},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            {"type": "TextInput", "name": INPUT_FIELD, "label": "${data.title}",
             "input-type": "text", "required": True},
            _footer(defn.label("continue", "Continue"),
                    {INPUT_FIELD: "${form." + INPUT_FIELD + "}"})]}}


def _display_screen(defn: FlowDefinition) -> dict[str, Any]:
    # ONE reused READ-ONLY screen for every {"display": true} step; the endpoint fills title + body
    # each time (like SELECT fills title + options). Nothing is collected — a TextBody shows the
    # computed text and the Footer just advances (data_exchange, empty payload; the engine advances a
    # display step off session state, reading no submitted field).
    return {
        "id": DISPLAY, "title": defn.label("display_title", "Information"),
        "data": {"title": {"type": "string", "__example__": "Your rewards"},
                 "body": {"type": "string",
                          "__example__": "Points: 120\nOffer: a free coffee on your next visit"},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            {"type": "TextBody", "text": "${data.body}"},
            _footer(defn.label("continue", "Continue"), {})]}}


def _calendar_screen(defn: FlowDefinition) -> dict[str, Any]:
    picker: dict[str, Any] = {
        "type": "CalendarPicker", "name": "date", "label": "${data.title}",
        "mode": defn.mode, "min-date": "${data.min_date}", "max-date": "${data.max_date}",
        "unavailable-dates": "${data.unavailable_dates}", "include-days": "${data.include_days}"}
    if defn.mode == "range":
        picker["min-days"] = defn.min_days
        picker["max-days"] = defn.max_days
    return {
        "id": CALENDAR, "title": defn.label("pick_date", "Choose a date"),
        "data": {"title": {"type": "string", "__example__": "Choose a date"},
                 "min_date": {"type": "string", "__example__": "2026-09-08"},
                 "max_date": {"type": "string", "__example__": "2026-10-08"},
                 "unavailable_dates": {"type": "array", "__example__": ["2026-09-10"]},
                 "include_days": {"type": "array", "__example__": ["Sun", "Mon", "Tue"]},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            picker, _footer(defn.label("continue", "Continue"), {"date": "${form.date}"})]}}


def _time_screen(defn: FlowDefinition) -> dict[str, Any]:
    return {
        "id": TIME, "title": defn.label("pick_time", "Choose a time"),
        "data": {"title": {"type": "string", "__example__": "Choose a time"},
                 "date": {"type": "string", "__example__": "2026-09-09"},
                 "slots": {"type": "array",
                           "__example__": [{"id": "2026-09-09T10:00", "title": "10:00"}]},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            {"type": "RadioButtonsGroup", "name": "time", "label": "${data.title}",
             "required": True, "data-source": "${data.slots}"},
            _footer(defn.label("continue", "Continue"), {"time": "${form.time}"})]}}


def _confirm_screen(defn: FlowDefinition) -> dict[str, Any]:
    return {
        "id": CONFIRM, "title": defn.label("confirm", "Confirm your booking"),
        "data": {"title": {"type": "string", "__example__": "Confirm your booking"},
                 "summary": {"type": "object", "__example__": {"service": "Haircut"}},
                 "error_message": {"type": "string", "__example__": "", "optional": True}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading", "text": "${data.title}"},
            _footer(defn.label("confirm_cta", "Confirm"), {"action": "confirm"})]}}


def _success_screen(defn: FlowDefinition) -> dict[str, Any]:
    # Terminal screens MUST carry a Footer (Meta rejects the upload otherwise). The engine finishes the
    # flow via the endpoint's extension_message_response, so this screen rarely renders; the Footer's
    # `complete` action is the schema-mandated close and the fallback if it ever shows.
    return {
        "id": SUCCESS, "title": defn.label("success_title", "Done"), "terminal": True, "success": True,
        "data": {},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextHeading",
             "text": defn.label("success_body", "Your booking is confirmed.")},
            {"type": "Footer", "label": defn.label("done", "Done"),
             "on-click-action": {"name": "complete", "payload": {}}}]}}


def build_booking_flow_json(defn: FlowDefinition) -> dict[str, Any]:
    """The companion Flow JSON for a flow definition, shaped to the steps it actually has: a SELECT
    and/or SELECT_MULTI screen for selection steps, an INPUT screen for free-text steps, and CALENDAR
    /TIME only when the definition includes date/time (a "happens now" flow has neither). CONFIRM +
    SUCCESS always close it."""
    dims = [s.get("dimension") for s in defn.steps]
    sel_steps = [s for s in defn.steps if s.get("dimension") not in ("date", "time", "confirm")]
    # A display step is neither a picker nor a text input — exclude it from both so it doesn't
    # conjure a phantom SELECT/INPUT screen.
    picker_steps = [s for s in sel_steps
                    if s.get("input") != "text" and s.get("display") is not True]
    has_single = any(not s.get("multi") for s in picker_steps)   # a SELECT (RadioButtonsGroup) screen
    has_multi = any(s.get("multi") for s in picker_steps)         # a SELECT_MULTI (CheckboxGroup) screen
    has_input = any(s.get("input") == "text" for s in sel_steps)  # an INPUT (TextInput) screen
    has_display = any(s.get("display") is True for s in sel_steps)  # a DISPLAY (read-only) screen
    has_date = "date" in dims
    has_time = "time" in dims

    screens: list[dict[str, Any]] = []
    routing: dict[str, list[str]] = {}
    pre_ids: list[str] = []                                   # every pre-commit step screen
    if has_single:
        screens.append(_select_screen(defn)); pre_ids.append(SELECT)
    if has_multi:
        screens.append(_select_multi_screen(defn)); pre_ids.append(SELECT_MULTI)
    if has_input:
        screens.append(_input_screen(defn)); pre_ids.append(INPUT)
    if has_display:
        screens.append(_display_screen(defn)); pre_ids.append(DISPLAY)
    # after the pre-commit steps comes the calendar if there is one, else straight to confirm
    after_pre = CALENDAR if has_date else CONFIRM
    for sid in pre_ids:
        # any pre-commit screen may re-render as the NEXT step (another selection/input) or move on
        routing[sid] = [*pre_ids, after_pre]
    if has_date:
        screens.append(_calendar_screen(defn))
        routing[CALENDAR] = [TIME] if has_time else [CONFIRM]
        if has_time:
            screens.append(_time_screen(defn))
            routing[TIME] = [CONFIRM]
    screens.append(_confirm_screen(defn))
    routing[CONFIRM] = [SUCCESS]
    screens.append(_success_screen(defn))

    return {"version": _VERSION, "data_api_version": _DATA_API_VERSION,
            "routing_model": routing, "screens": screens}


class _RescheduleDefn:
    """A minimal FlowDefinition-shaped label/mode source so the reschedule Flow JSON reuses the SAME
    screen builders as booking (no drift). RescheduleEngine isn't step-driven, so it has no real
    FlowDefinition — only its i18n strings and a fixed single-date mode."""
    mode = "single"
    min_days = 1
    max_days = 14
    default_working_days: tuple[str, ...] = ()

    def __init__(self, strings: dict[str, str] | None = None) -> None:
        self._s = strings or {}

    def label(self, key: str, default: str) -> str:
        return self._s.get(key, default)


def build_reschedule_flow_json(strings: dict[str, str] | None = None) -> dict[str, Any]:
    """The companion Flow JSON for RescheduleEngine — a FIXED, branching machine (not step-driven):
    SELECT (the appointment list, then reused for the reschedule/cancel action choice) → CALENDAR →
    TIME → CONFIRM (also the cancel confirmation) → SUCCESS. Reuses booking's screen builders so the
    two never drift; the routing_model reflects the reschedule branches (from SELECT: re-render as the
    action choice, go to CALENDAR for reschedule, or CONFIRM for cancel)."""
    defn = _RescheduleDefn(strings)
    screens = [_select_screen(defn), _calendar_screen(defn), _time_screen(defn),
               _confirm_screen(defn), _success_screen(defn)]
    routing = {
        SELECT: [SELECT, CALENDAR, CONFIRM],   # list→action re-render, reschedule→date, cancel→confirm
        CALENDAR: [TIME],
        TIME: [CONFIRM],
        CONFIRM: [SUCCESS],
    }
    return {"version": _VERSION, "data_api_version": _DATA_API_VERSION,
            "routing_model": routing, "screens": screens}


class FlowPreviewUnavailable(ValueError):
    """No Flow JSON can be produced for this handler. `str()` is the 404 reason code
    (NO_SUCH_FLOW / NO_DEFINITION)."""


async def build_flow_preview_json(handler: Any) -> dict[str, Any]:
    """The companion Flow JSON for any previewable engine, dispatched by type — the ONE place both the
    internal and owner-facing preview routes go through, so they stay identical. A BookingEngine
    (booking/delivery/visit/…) renders from its config definition; a RescheduleEngine renders its fixed
    machine. Raises FlowPreviewUnavailable (→ 404) for an unknown/non-previewable engine or a booking
    engine with no definition configured yet."""
    # Imported here (not at module top) to avoid any import-order coupling with the engine modules.
    from app.core.booking_flow import BookingEngine
    from app.core.reschedule_flow import RescheduleEngine
    if isinstance(handler, BookingEngine):
        defn = await handler.definition()
        if defn is None:
            raise FlowPreviewUnavailable("NO_DEFINITION")
        return build_booking_flow_json(defn)
    if isinstance(handler, RescheduleEngine):
        return build_reschedule_flow_json(handler.strings())
    raise FlowPreviewUnavailable("NO_SUCH_FLOW")
