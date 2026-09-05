"""The interactive-message builder: correct Meta shape for buttons and lists, and every limit the
Cloud API enforces is refused HERE (at build time) with a clear reason — because a rejected send is
a customer left waiting, and the reply ids are the safety anchor a state change keys on."""
from __future__ import annotations

import pytest

from app.core.whatsapp_interactive import (InteractiveError, build_button, build_flow, build_list)


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


# ── flow — the post-visit rating app's own message shape ──────────────────────
def test_flow_shape_matches_the_cloud_api():
    obj = build_flow(body="How was your visit?", flow_token="rate:vis_1:abc",
                     flow_id="123456789", flow_cta="Rate us", screen="RATING",
                     data={"customer_name": "Ahmed"}, footer="Raje3")
    assert obj["type"] == "flow"
    assert obj["body"] == {"text": "How was your visit?"}
    assert obj["footer"] == {"text": "Raje3"}
    assert "header" not in obj                                    # omitted when not given
    params = obj["action"]["parameters"]
    assert obj["action"]["name"] == "flow"
    assert params["flow_message_version"] == "3"
    assert params["flow_token"] == "rate:vis_1:abc"
    assert params["flow_id"] == "123456789"
    assert params["flow_cta"] == "Rate us"
    assert params["flow_action"] == "navigate"
    assert params["flow_action_payload"] == {"screen": "RATING", "data": {"customer_name": "Ahmed"}}


def test_flow_data_defaults_to_an_empty_dict_never_omitted():
    obj = build_flow(body="x", flow_token="t1", flow_id="f1", flow_cta="Go", screen="S1")
    assert obj["action"]["parameters"]["flow_action_payload"]["data"] == {}


def test_flow_requires_every_one_of_its_own_fields():
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="", flow_id="f1", flow_cta="Go", screen="S1")
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="t1", flow_id="", flow_cta="Go", screen="S1")
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="t1", flow_id="f1", flow_cta="", screen="S1")
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="t1", flow_id="f1", flow_cta="Go", screen="")
    with pytest.raises(InteractiveError):
        build_flow(body="", flow_token="t1", flow_id="f1", flow_cta="Go", screen="S1")


def test_flow_cta_over_twenty_characters_is_refused():
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="t1", flow_id="f1", flow_cta="a" * 21, screen="S1")


def test_flow_token_over_the_cap_is_refused():
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="t" * 257, flow_id="f1", flow_cta="Go", screen="S1")


def test_two_flow_sends_with_different_tokens_are_distinguishable():
    a = build_flow(body="x", flow_token="rate:vis_1", flow_id="f1", flow_cta="Go", screen="S1")
    b = build_flow(body="x", flow_token="rate:vis_2", flow_id="f1", flow_cta="Go", screen="S1")
    assert a["action"]["parameters"]["flow_token"] != b["action"]["parameters"]["flow_token"]
