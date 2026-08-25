"""The interactive-message builder: correct Meta shape for buttons and lists, and every limit the
Cloud API enforces is refused HERE (at build time) with a clear reason — because a rejected send is
a customer left waiting, and the reply ids are the safety anchor a state change keys on."""
from __future__ import annotations

import pytest

from app.core.whatsapp_interactive import (InteractiveError, build_button, build_list)


def test_button_shape_matches_the_cloud_api():
    obj = build_button(body="Confirm your booking?", header="Booking", footer="Raje3",
                       buttons=[("confirm", "Confirm"), ("cancel", "Cancel")])
    assert obj["type"] == "button"
    assert obj["body"] == {"text": "Confirm your booking?"}
    assert obj["header"] == {"type": "text", "text": "Booking"}
    assert obj["footer"] == {"text": "Raje3"}
    assert obj["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}},
        {"type": "reply", "reply": {"id": "cancel", "title": "Cancel"}}]


def test_button_limits_and_uniqueness():
    with pytest.raises(InteractiveError):
        build_button(body="x", buttons=[("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])  # >3
    with pytest.raises(InteractiveError):
        build_button(body="x", buttons=[("dup", "One"), ("dup", "Two")])                  # dup id
    with pytest.raises(InteractiveError):
        build_button(body="x", buttons=[("id", "a" * 21)])                                # title >20
    with pytest.raises(InteractiveError):
        build_button(body="x", buttons=[])                                                # none
    with pytest.raises(InteractiveError):
        build_button(body="", buttons=[("id", "ok")])                                     # empty body


def test_list_shape_matches_the_cloud_api():
    obj = build_list(body="Pick a slot", button_text="Choose",
                     sections=[{"title": "Morning",
                                "rows": [{"id": "s9", "title": "09:00", "description": "with Dr A"},
                                         {"id": "s10", "title": "10:00"}]}])
    assert obj["type"] == "list"
    assert obj["action"]["button"] == "Choose"
    sec = obj["action"]["sections"][0]
    assert sec["title"] == "Morning"
    assert sec["rows"][0] == {"id": "s9", "title": "09:00", "description": "with Dr A"}
    assert sec["rows"][1] == {"id": "s10", "title": "10:00"}      # description omitted when absent


def test_list_limits_and_uniqueness():
    with pytest.raises(InteractiveError):
        build_list(body="x", button_text="Go", sections=[])                               # no section
    with pytest.raises(InteractiveError):
        build_list(body="x", button_text="", sections=[{"rows": [{"id": "a", "title": "A"}]}])  # no btn
    with pytest.raises(InteractiveError):                                                  # >10 rows total
        build_list(body="x", button_text="Go",
                   sections=[{"rows": [{"id": f"r{i}", "title": str(i)} for i in range(11)]}])
    with pytest.raises(InteractiveError):                                                  # dup row id
        build_list(body="x", button_text="Go",
                   sections=[{"rows": [{"id": "same", "title": "A"}, {"id": "same", "title": "B"}]}])
    with pytest.raises(InteractiveError):                                                  # row title >24
        build_list(body="x", button_text="Go",
                   sections=[{"rows": [{"id": "a", "title": "t" * 25}]}])
