"""The Flow JSON generator matches the booking engine's screen contract (they can't drift)."""
from __future__ import annotations

from app.core.booking_flow import CALENDAR, CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, TIME, FlowDefinition
from app.core.flow_json import build_booking_flow_json


def _screen(js, sid):
    return next((s for s in js["screens"] if s["id"] == sid), None)


def _full_defn():
    return FlowDefinition(steps=({"dimension": "service"}, {"dimension": "staff"},
                                 {"dimension": "date"}, {"dimension": "time"},
                                 {"dimension": "confirm"}))


def test_generates_the_five_screens_and_versions():
    js = build_booking_flow_json(_full_defn())
    assert js["version"] and js["data_api_version"]
    ids = {s["id"] for s in js["screens"]}
    assert ids == {SELECT, CALENDAR, TIME, CONFIRM, SUCCESS}
    # SUCCESS is the terminal screen
    assert _screen(js, SUCCESS)["terminal"] is True


def test_version_supports_calendarpicker_and_terminal_has_footer():
    # CalendarPicker requires Flow JSON >=6.1 and Meta no longer publishes <=5.0, so an obsolete
    # version would make Meta reject the upload. And every terminal screen MUST carry a Footer.
    js = build_booking_flow_json(_full_defn())
    major = float(js["version"])
    assert major >= 6.1, "Flow JSON version must support CalendarPicker (>=6.1)"
    success = _screen(js, SUCCESS)
    footer = next((c for c in success["layout"]["children"] if c["type"] == "Footer"), None)
    assert footer is not None, "terminal screens must have a Footer"
    assert footer["on-click-action"]["name"] == "complete"


def test_select_field_name_matches_the_engine():
    js = build_booking_flow_json(_full_defn())
    radio = next(c for c in _screen(js, SELECT)["layout"]["children"]
                 if c["type"] == "RadioButtonsGroup")
    assert radio["name"] == SELECTION_FIELD                 # same constant the engine reads
    footer = next(c for c in _screen(js, SELECT)["layout"]["children"] if c["type"] == "Footer")
    assert footer["on-click-action"]["name"] == "data_exchange"
    assert footer["on-click-action"]["payload"][SELECTION_FIELD] == "${form." + SELECTION_FIELD + "}"


def test_calendar_binds_constraints_dynamically():
    cal = _screen(build_booking_flow_json(_full_defn()), CALENDAR)
    picker = next(c for c in cal["layout"]["children"] if c["type"] == "CalendarPicker")
    assert picker["unavailable-dates"] == "${data.unavailable_dates}"
    assert picker["include-days"] == "${data.include_days}"


def test_no_selection_steps_omits_the_select_screen():
    js = build_booking_flow_json(FlowDefinition(steps=(
        {"dimension": "date"}, {"dimension": "time"}, {"dimension": "confirm"})))
    assert _screen(js, SELECT) is None and _screen(js, CALENDAR) is not None


def test_range_mode_has_no_time_screen_and_sets_min_max_days():
    js = build_booking_flow_json(FlowDefinition(
        steps=({"dimension": "service"}, {"dimension": "date"}, {"dimension": "confirm"}),
        mode="range", min_days=2, max_days=10))
    assert _screen(js, TIME) is None
    picker = next(c for c in _screen(js, CALENDAR)["layout"]["children"]
                  if c["type"] == "CalendarPicker")
    assert picker["mode"] == "range" and picker["min-days"] == 2 and picker["max-days"] == 10
    assert js["routing_model"][CALENDAR] == [CONFIRM]
