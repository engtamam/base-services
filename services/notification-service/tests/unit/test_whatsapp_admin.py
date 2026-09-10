"""WhatsApp management calls — PIN set/rotate, subscribed-apps read, business-verification read.

The Meta HTTP layer is stubbed, so these pin the exact Graph call SHAPE (endpoint + params) and the
parsing, without touching the network. Verified against Meta docs 2026-09-10.
"""
from __future__ import annotations

import json

from app.core import whatsapp_admin as wa


def test_set_two_step_pin_posts_pin_to_the_phone_node(monkeypatch):
    seen = {}

    def fake_post(url, payload, headers):
        seen["url"], seen["payload"] = url, payload
        return 200, "{}"
    monkeypatch.setattr(wa, "post_json", fake_post)
    ok, reason = wa.set_two_step_pin(access_token="tok", phone_number_id="PN1", pin="123456",
                                     graph_base="https://graph.facebook.com/v26.0")
    assert ok and reason == ""
    assert seen["url"] == "https://graph.facebook.com/v26.0/PN1"       # the phone node, not /register
    assert seen["payload"] == {"pin": "123456"}


def test_set_two_step_pin_classifies_a_meta_error(monkeypatch):
    monkeypatch.setattr(wa, "post_json",
                        lambda u, p, h: (400, json.dumps({"error": {"code": 133016}})))
    ok, reason = wa.set_two_step_pin(access_token="t", phone_number_id="PN1", pin="000000",
                                     graph_base="")
    assert ok is False and reason == "RATE_LIMITED"


def test_fetch_subscribed_apps_reads_the_waba_node(monkeypatch):
    seen = {}

    def fake_get(url, params, headers):
        seen["url"] = url
        return 200, json.dumps({"data": [{"whatsapp_business_api_data": {"link": "x"},
                                          "subscribed_fields": ["messages", "message_status"]}]})
    monkeypatch.setattr(wa, "get_json", fake_get)
    res = wa.fetch_subscribed_apps(access_token="t", waba_id="WABA1", graph_base="")
    assert seen["url"].endswith("/WABA1/subscribed_apps")
    assert res["ok"] and res["apps"] and res["apps"][0]["subscribed_fields"] == ["messages", "message_status"]


def test_fetch_business_verification_asks_for_the_right_field(monkeypatch):
    seen = {}

    def fake_get(url, params, headers):
        seen["url"], seen["params"] = url, params
        return 200, json.dumps({"business_verification_status": "verified", "status": "CONNECTED"})
    monkeypatch.setattr(wa, "get_json", fake_get)
    res = wa.fetch_business_verification(access_token="t", waba_id="WABA1", graph_base="")
    assert seen["url"].endswith("/WABA1")
    assert "business_verification_status" in seen["params"]["fields"]   # the REAL field, not account_review_status
    assert res["business_verification_status"] == "verified" and res["status"] == "CONNECTED"


def test_reads_surface_a_network_failure_as_unreachable(monkeypatch):
    monkeypatch.setattr(wa, "get_json", lambda u, p, h: (0, "request failed"))
    assert wa.fetch_subscribed_apps(access_token="t", waba_id="W", graph_base="")["reason"] == "UNREACHABLE"
    assert wa.fetch_business_verification(access_token="t", waba_id="W", graph_base="")["reason"] == "UNREACHABLE"
