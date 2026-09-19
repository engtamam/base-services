"""The interactive-message builder: correct Meta shape for buttons and lists, and every limit the
Cloud API enforces is refused HERE (at build time) with a clear reason — because a rejected send is
a customer left waiting, and the reply ids are the safety anchor a state change keys on."""
from __future__ import annotations

import pytest

from app.core.whatsapp_interactive import (InteractiveError, build_button, build_flow, build_list,
                                           build_location_request, build_product,
                                           build_product_list, parse_order, build_order_details,
                                           build_order_status, parse_payment_status)


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


# ── product messages (commerce Phase 3) — single + multi + inbound order parsing ─────────────


def test_single_product_shape_matches_the_cloud_api():
    obj = build_product(catalog_id="CAT1", product_retailer_id="SKU-9", body="Our best seller")
    assert obj == {
        "type": "product",
        "body": {"text": "Our best seller"},
        "action": {"catalog_id": "CAT1", "product_retailer_id": "SKU-9"}}


def test_single_product_omits_empty_body_and_footer_and_takes_no_header():
    obj = build_product(catalog_id="CAT1", product_retailer_id="SKU-9")
    assert obj == {"type": "product",
                   "action": {"catalog_id": "CAT1", "product_retailer_id": "SKU-9"}}
    assert "header" not in obj and "body" not in obj and "footer" not in obj


def test_single_product_requires_catalog_and_retailer_ids():
    with pytest.raises(InteractiveError):
        build_product(catalog_id="", product_retailer_id="SKU-9")
    with pytest.raises(InteractiveError):
        build_product(catalog_id="CAT1", product_retailer_id="  ")


def test_multi_product_shape_matches_the_cloud_api():
    obj = build_product_list(
        catalog_id="CAT1", header="This week", body="Pick what you like",
        sections=[{"title": "Coffee", "product_ids": ["SKU-1", "SKU-2"]},
                  {"title": "Tea", "product_ids": ["SKU-3"]}])
    assert obj == {
        "type": "product_list",
        "header": {"type": "text", "text": "This week"},
        "body": {"text": "Pick what you like"},
        "action": {"catalog_id": "CAT1", "sections": [
            {"title": "Coffee", "product_items": [
                {"product_retailer_id": "SKU-1"}, {"product_retailer_id": "SKU-2"}]},
            {"title": "Tea", "product_items": [{"product_retailer_id": "SKU-3"}]}]}}


def test_multi_product_single_section_title_optional():
    obj = build_product_list(catalog_id="CAT1", header="Menu", body="Choose",
                             sections=[{"product_ids": ["SKU-1"]}])
    assert obj["action"]["sections"] == [{"product_items": [{"product_retailer_id": "SKU-1"}]}]


def test_multi_product_requires_title_when_multiple_sections():
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="h", body="b",
                           sections=[{"product_ids": ["SKU-1"]},
                                     {"product_ids": ["SKU-2"]}])


def test_multi_product_requires_header_and_body():
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="", body="b",
                           sections=[{"product_ids": ["SKU-1"]}])
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="h", body="",
                           sections=[{"product_ids": ["SKU-1"]}])


def test_multi_product_caps_at_thirty_products_total():
    ids = [f"SKU-{i}" for i in range(31)]
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="h", body="b",
                           sections=[{"title": "All", "product_ids": ids}])
    # exactly 30 is allowed
    ok = build_product_list(catalog_id="CAT1", header="h", body="b",
                            sections=[{"title": "All", "product_ids": ids[:30]}])
    assert sum(len(s["product_items"]) for s in ok["action"]["sections"]) == 30


def test_multi_product_rejects_empty_section_and_blank_product_id():
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="h", body="b",
                           sections=[{"title": "x", "product_ids": []}])
    with pytest.raises(InteractiveError):
        build_product_list(catalog_id="CAT1", header="h", body="b",
                           sections=[{"product_ids": ["  "]}])


def test_parse_order_normalises_quantity_price_and_totals():
    msg = {"type": "order", "from": "9665550001", "id": "wamid.X",
           "order": {"catalog_id": "CAT1", "text": "please deliver tonight",
                     "product_items": [
                         {"product_retailer_id": "SKU-1", "quantity": "2",
                          "item_price": 12.5, "currency": "SAR"},
                         {"product_retailer_id": "SKU-2", "quantity": 1,
                          "item_price": 4.0, "currency": "SAR"}]}}
    out = parse_order(msg)
    assert out["catalog_id"] == "CAT1"
    assert out["from"] == "9665550001" and out["wamid"] == "wamid.X"
    assert out["text"] == "please deliver tonight"
    assert out["items"][0] == {"product_retailer_id": "SKU-1", "quantity": 2,
                               "item_price": 12.5, "currency": "SAR", "line_total": 25.0}
    assert out["order_total"] == 29.0


def test_parse_order_returns_none_for_non_order_messages():
    assert parse_order({"type": "text", "text": {"body": "hi"}}) is None
    assert parse_order({}) is None
    assert parse_order("not a dict") is None


def test_parse_order_survives_malformed_quantity_or_price():
    msg = {"type": "order", "order": {"catalog_id": "C", "product_items": [
        {"product_retailer_id": "SKU-1", "quantity": "abc", "item_price": None, "currency": "SAR"}]}}
    out = parse_order(msg)
    assert out["items"][0]["quantity"] == 0 and out["items"][0]["item_price"] == 0.0
    assert out["order_total"] == 0.0


# ── native pay: order_details / order_status (build-ahead, KSA-dormant) ───────────────────────


def _items():
    return [{"retailer_id": "SKU-1", "name": "Coffee", "amount_minor": 10000, "quantity": 1},
            {"retailer_id": "SKU-2", "name": "Tea", "amount_minor": 5000, "quantity": 2}]


def test_order_details_shape_and_offset_amounts():
    obj = build_order_details(
        reference_id="order-1", currency="sar", order_type="physical-goods", items=_items(),
        subtotal_minor=20000, tax_minor=1000, shipping_minor=500, discount_minor=500,
        payment_gateway="tuwaiqpay", payment_configuration="cfg-1", body="Pay please")
    assert obj["type"] == "order_details"
    p = obj["action"]["parameters"]
    assert obj["action"]["name"] == "review_and_pay"
    assert p["reference_id"] == "order-1" and p["currency"] == "SAR" and p["type"] == "physical-goods"
    # total = subtotal + tax + shipping - discount = 20000+1000+500-500 = 21000
    assert p["total_amount"] == {"value": 21000, "offset": 100}
    assert p["order"]["subtotal"] == {"value": 20000, "offset": 100}
    assert p["payment_settings"][0]["payment_gateway"] == {"type": "tuwaiqpay",
                                                           "configuration_name": "cfg-1"}
    assert p["order"]["items"][0]["amount"] == {"value": 10000, "offset": 100}
    assert p["order"]["status"] == "pending"


def test_order_details_subtotal_must_equal_item_sum():
    with pytest.raises(InteractiveError):
        build_order_details(reference_id="o1", currency="SAR", order_type="digital-goods",
                            items=_items(), subtotal_minor=99999,  # wrong (should be 20000)
                            payment_gateway="g", payment_configuration="c")


def test_order_details_rejects_bad_ref_type_and_empty_items():
    with pytest.raises(InteractiveError):
        build_order_details(reference_id="x"*36, currency="SAR", order_type="digital-goods",
                            items=_items(), subtotal_minor=20000, payment_gateway="g",
                            payment_configuration="c")
    with pytest.raises(InteractiveError):
        build_order_details(reference_id="o1", currency="SAR", order_type="bogus",
                            items=_items(), subtotal_minor=20000, payment_gateway="g",
                            payment_configuration="c")
    with pytest.raises(InteractiveError):
        build_order_details(reference_id="o1", currency="SAR", order_type="digital-goods",
                            items=[], subtotal_minor=0, payment_gateway="g", payment_configuration="c")


def test_order_details_3dp_offset():
    obj = build_order_details(
        reference_id="o-kwd", currency="KWD", order_type="digital-goods",
        items=[{"retailer_id": "S", "name": "X", "amount_minor": 1234, "quantity": 1}],
        subtotal_minor=1234, payment_gateway="g", payment_configuration="c", offset=1000)
    assert obj["action"]["parameters"]["total_amount"] == {"value": 1234, "offset": 1000}


def test_order_status_shape_and_enum():
    obj = build_order_status(reference_id="order-1", status="shipped", description="on its way")
    assert obj["type"] == "order_status" and obj["action"]["name"] == "review_order"
    assert obj["action"]["parameters"] == {"reference_id": "order-1",
                                           "order": {"status": "shipped", "description": "on its way"}}
    with pytest.raises(InteractiveError):
        build_order_status(reference_id="o1", status="not-a-status")


def test_parse_payment_status_normalises_and_marks_unverified():
    st = {"type": "payment", "status": "captured", "recipient_id": "9665",
          "payment": {"reference_id": "order-1", "currency": "SAR",
                      "amount": {"value": 21000, "offset": 100},
                      "transaction": {"id": "tx", "pg_transaction_id": "pg", "status": "success"}}}
    out = parse_payment_status(st)
    assert out["reference_id"] == "order-1" and out["amount_minor"] == 21000 and out["offset"] == 100
    assert out["status"] == "captured" and out["transaction_id"] == "tx"
    assert out["verified"] is False                       # must be confirmed via lookup, never trusted
    assert parse_payment_status({"type": "delivered"}) is None
