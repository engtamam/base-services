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

from app.core.booking_flow import (CALENDAR, CONFIRM, SELECT, SELECT_MULTI, SELECTION_FIELD, SUCCESS,
                                   TIME, FlowDefinition)

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
    """The companion Flow JSON for a booking definition. Includes the SELECT screen only if the
    definition has selection steps, and the TIME screen only in single mode."""
    dims = [s.get("dimension") for s in defn.steps]
    sel_steps = [s for s in defn.steps if s.get("dimension") not in ("date", "time", "confirm")]
    has_single = any(not s.get("multi") for s in sel_steps)   # a SELECT (RadioButtonsGroup) screen
    has_multi = any(s.get("multi") for s in sel_steps)         # a SELECT_MULTI (CheckboxGroup) screen
    has_time = "time" in dims

    screens: list[dict[str, Any]] = []
    routing: dict[str, list[str]] = {}
    sel_ids: list[str] = []
    if has_single:
        screens.append(_select_screen(defn)); sel_ids.append(SELECT)
    if has_multi:
        screens.append(_select_multi_screen(defn)); sel_ids.append(SELECT_MULTI)
    for sid in sel_ids:
        # any selection screen may re-render as the NEXT selection (single or multi), or move on to date
        routing[sid] = [*sel_ids, CALENDAR]
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
