"""The interactive-message builder: correct Meta shape for buttons and lists, and every limit the
Cloud API enforces is refused HERE (at build time) with a clear reason — because a rejected send is
a customer left waiting, and the reply ids are the safety anchor a state change keys on."""
from __future__ import annotations

import pytest

from app.core.whatsapp_interactive import (InteractiveError, build_button, build_flow, build_list,
                                           build_location_request)


def test_location_request_shape_matches_the_cloud_api():
    obj = build_location_request(body="Share your location so we can find the nearest branch")
    assert obj == {
        "type": "location_request_message",
        "body": {"text": "Share your location so we can find the nearest branch"},
        "action": {"name": "send_location"}}


def test_location_request_needs_body_and_caps_it():
    with pytest.raises(InteractiveError):
        build_location_request(body="")
    with pytest.raises(InteractiveError):
        build_location_request(body="   ")
    with pytest.raises(InteractiveError):
        build_location_request(body="x" * 1025)   # body ≤1024


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


def test_button_with_an_image_media_header():
    obj = build_button(body="This week's cut", buttons=[("book", "Book")],
                       header_media={"type": "image", "media_id": "MID-1"})
    assert obj["header"] == {"type": "image", "image": {"id": "MID-1"}}


def test_button_with_a_video_media_header():
    obj = build_button(body="Watch", buttons=[("book", "Book")],
                       header_media={"type": "video", "media_id": "V9"})
    assert obj["header"] == {"type": "video", "video": {"id": "V9"}}


def test_button_media_header_and_text_header_are_mutually_exclusive():
    with pytest.raises(InteractiveError):
        build_button(body="x", buttons=[("id", "ok")], header="Title",
                     header_media={"type": "image", "media_id": "M"})


def test_button_media_header_validates_type_and_media_id():
    with pytest.raises(InteractiveError):     # bad media type
        build_button(body="x", buttons=[("id", "ok")],
                     header_media={"type": "audio", "media_id": "M"})
    with pytest.raises(InteractiveError):     # missing media_id
        build_button(body="x", buttons=[("id", "ok")], header_media={"type": "image"})


def test_button_without_a_header_media_is_unchanged():
    obj = build_button(body="x", buttons=[("id", "ok")])
    assert "header" not in obj                 # identical to today when neither header is given


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


def test_flow_can_be_identified_by_name_the_waba_portable_id():
    # A Flow cloned into each tenant's WABA keeps its name but gets a new id, so flow_name is what a
    # single send definition can use across every tenant. Meta: flow_id/flow_name are mutually
    # exclusive, exactly one required (verified vs the send-a-flow guide, Sep 2026).
    obj = build_flow(body="x", flow_token="t1", flow_name="rating_flow", flow_cta="Go", screen="S1")
    params = obj["action"]["parameters"]
    assert params["flow_name"] == "rating_flow" and "flow_id" not in params


def test_flow_requires_exactly_one_of_id_or_name():
    with pytest.raises(InteractiveError):    # neither
        build_flow(body="x", flow_token="t1", flow_cta="Go", screen="S1")
    with pytest.raises(InteractiveError):    # both
        build_flow(body="x", flow_token="t1", flow_id="f1", flow_name="n1", flow_cta="Go", screen="S1")


def test_flow_data_is_omitted_when_empty_never_sent_as_empty_object():
    # Meta: flow_action_payload.data is optional but MUST be non-empty if present — so it is omitted
    # entirely when there is no seed data (sending an empty {} is rejected). Verified vs Meta's own
    # send-a-flow guide, Sep 2026.
    obj = build_flow(body="x", flow_token="t1", flow_id="f1", flow_cta="Go", screen="S1")
    assert "data" not in obj["action"]["parameters"]["flow_action_payload"]


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


# ── data_exchange (dynamic, endpoint-driven) flows — e.g. the booking calendar ──
def test_data_exchange_flow_may_omit_the_screen():
    from app.core.whatsapp_interactive import build_flow
    msg = build_flow(body="Book an appointment", flow_token="tk1", flow_cta="Book",
                     flow_name="booking", flow_action="data_exchange")
    params = msg["action"]["parameters"]
    assert params["flow_action"] == "data_exchange"
    # the endpoint's INIT supplies the first screen → no flow_action_payload sent at all
    assert "flow_action_payload" not in params
    assert params["flow_name"] == "booking" and params["flow_message_version"] == "3"


def test_data_exchange_flow_keeps_seed_data_when_given():
    from app.core.whatsapp_interactive import build_flow
    msg = build_flow(body="Book", flow_token="tk", flow_cta="Book", flow_id="123",
                     flow_action="data_exchange", data={"tenant": "shop_a"})
    payload = msg["action"]["parameters"]["flow_action_payload"]
    assert payload == {"data": {"tenant": "shop_a"}}          # data kept, screen omitted


def test_navigate_still_requires_a_screen():
    import pytest
    from app.core.whatsapp_interactive import InteractiveError, build_flow
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="tk", flow_cta="Go", flow_name="f")   # navigate, no screen


def test_a_bad_flow_action_is_refused():
    import pytest
    from app.core.whatsapp_interactive import InteractiveError, build_flow
    with pytest.raises(InteractiveError):
        build_flow(body="x", flow_token="tk", flow_cta="Go", flow_name="f", screen="S",
                   flow_action="stream")
