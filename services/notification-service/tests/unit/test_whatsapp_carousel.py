"""build_carousel_send — the send-time component tree for an approved carousel template.

Correct Meta shape (card_index int, button index string, header media by id, quick_reply payload vs
url text) and every caller-fixable error raised HERE, at build time, not as a Meta 400.
"""
from __future__ import annotations

import pytest

from app.core.whatsapp_carousel import CarouselSendError, build_carousel_send


def test_shape_matches_the_cloud_api():
    obj = build_carousel_send([
        {"media_id": "m0", "media_type": "image", "body_params": ["Pizza"],
         "buttons": [{"sub_type": "quick_reply", "payload": "order:0"},
                     {"sub_type": "url", "text": "menu-0"}]},
        {"media_id": "m1", "media_type": "image", "body_params": ["Burger"],
         "buttons": [{"sub_type": "quick_reply", "payload": "order:1"},
                     {"sub_type": "url", "text": "menu-1"}]}])
    assert obj["type"] == "carousel" and len(obj["cards"]) == 2
    c0 = obj["cards"][0]
    assert c0["card_index"] == 0                              # integer
    hdr = c0["components"][0]
    assert hdr == {"type": "header", "parameters": [{"type": "image", "image": {"id": "m0"}}]}
    body = c0["components"][1]
    assert body == {"type": "body", "parameters": [{"type": "text", "text": "Pizza"}]}
    qr = c0["components"][2]
    assert qr == {"type": "button", "sub_type": "quick_reply", "index": "0",
                  "parameters": [{"type": "payload", "payload": "order:0"}]}
    url = c0["components"][3]
    assert url == {"type": "button", "sub_type": "url", "index": "1",
                   "parameters": [{"type": "text", "text": "menu-0"}]}
    assert obj["cards"][1]["card_index"] == 1


def test_video_header_uses_the_video_parameter():
    obj = build_carousel_send([{"media_id": "v0", "media_type": "video", "buttons": []}])
    assert obj["cards"][0]["components"][0] == {
        "type": "header", "parameters": [{"type": "video", "video": {"id": "v0"}}]}


def test_a_card_needs_a_media_id():
    with pytest.raises(CarouselSendError):
        build_carousel_send([{"media_type": "image", "buttons": []}])


def test_bad_media_type_is_refused():
    with pytest.raises(CarouselSendError):
        build_carousel_send([{"media_id": "m", "media_type": "gif", "buttons": []}])


def test_bad_button_sub_type_is_refused():
    with pytest.raises(CarouselSendError):
        build_carousel_send([{"media_id": "m", "buttons": [{"sub_type": "flow", "payload": "x"}]}])


def test_empty_cards_is_refused():
    with pytest.raises(CarouselSendError):
        build_carousel_send([])
