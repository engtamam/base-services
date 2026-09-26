"""Audit FIN-019 — template dynamic buttons must be indexed by position, not all hardcoded "0".

A single dynamic button is index "0" (unchanged); when more than one is supplied in one send, each
takes the next index in declaration order, or Meta rejects/misroutes the message.
"""
from __future__ import annotations

import pytest

from app.adapters.whatsapp.meta import MetaWhatsApp


def _gw(capture):
    g = MetaWhatsApp(access_token="tok", phone_number_id="pid")
    g._post = lambda payload: capture.update(payload) or "wamid.1"   # capture, don't hit the network
    return g


def _buttons(cap):
    return [c for c in cap["template"].get("components", []) if c.get("type") == "button"]


def test_a_single_url_button_is_index_0():
    cap: dict = {}
    _gw(cap).send(to="+9665", template_name="t", language="ar", variables=[],
                  url_button_param="tok123")
    btns = _buttons(cap)
    assert len(btns) == 1 and btns[0]["sub_type"] == "url" and btns[0]["index"] == "0"


def test_two_dynamic_buttons_get_distinct_indices_in_order():
    cap: dict = {}
    _gw(cap).send(to="+9665", template_name="t", language="ar", variables=[],
                  button_payload="BOOK", url_button_param="paytok")
    btns = _buttons(cap)
    assert [(b["sub_type"], b["index"]) for b in btns] == [("quick_reply", "0"), ("url", "1")]


def test_three_dynamic_buttons_index_0_1_2():
    cap: dict = {}
    _gw(cap).send(to="+9665", template_name="t", language="ar", variables=[],
                  flow_token="ft", button_payload="BOOK", url_button_param="paytok")
    btns = _buttons(cap)
    assert [b["index"] for b in btns] == ["0", "1", "2"]
    assert [b["sub_type"] for b in btns] == ["flow", "quick_reply", "url"]
