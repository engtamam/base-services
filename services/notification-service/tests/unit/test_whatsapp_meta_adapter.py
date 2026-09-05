"""MetaTemplateManager — the driver that talks to Graph. The one question: does it send Meta a
request Meta will accept, and read back a status an operator can act on?

The blocking failure this guards: Meta refuses any parameterised template that arrives without an
example value per parameter ("Upon template creation, you must include an example value for each
parameter"). Every real template has a placeholder, so a submit with no example burned the template
NAME — which cannot be reused after approval — on a 400 discovered hours later.
"""
from __future__ import annotations

import json

import pytest

from app.adapters.whatsapp_templates import meta as meta_mod
from app.adapters.whatsapp_templates.meta import MetaTemplateManager

_BASE = "https://graph.facebook.test/v20.0"


def _mgr():
    return MetaTemplateManager(access_token="tok", waba_id="WABA1", base_url=_BASE)


def _capture(monkeypatch, response=(200, json.dumps({"id": "meta:1", "status": "PENDING"}))):
    """Intercept the Graph POST and record the URL + payload the adapter sends."""
    sent = {}

    def fake_post_json(url, payload, headers):
        sent["url"], sent["payload"], sent["headers"] = url, payload, headers
        return response

    monkeypatch.setattr(meta_mod, "post_json", fake_post_json)
    return sent


def test_submit_includes_an_example_value_for_every_parameter(monkeypatch):
    sent = _capture(monkeypatch)
    res = _mgr().submit(name="order_ready", language="ar", category="UTILITY",
                        body="أهلاً {{1}}، طلبك من {{2}} جاهز.",
                        variables=["customer_name", "shop_name"],
                        examples=["محمد", "مغسلة النخيل"])
    assert res.ok and res.provider_template_id == "meta:1"
    body_comp = next(c for c in sent["payload"]["components"] if c["type"] == "BODY")
    # Meta's exact positional shape: example.body_text is a LIST of example SETS.
    assert body_comp["example"] == {"body_text": [["محمد", "مغسلة النخيل"]]}
    assert sent["url"] == f"{_BASE}/WABA1/message_templates"


def test_submit_falls_back_to_the_parameter_name_when_an_example_is_missing(monkeypatch):
    # A weak example still submits; the alternative is a hard 400 that burns the template name.
    sent = _capture(monkeypatch)
    _mgr().submit(name="t", language="en", category="UTILITY", body="Hi {{1}} and {{2}}",
                  variables=["first", "second"], examples=["Pablo"])   # only one example given
    body_comp = next(c for c in sent["payload"]["components"] if c["type"] == "BODY")
    assert body_comp["example"] == {"body_text": [["Pablo", "second"]]}   # 2nd falls back to name


def test_submit_omits_the_example_when_the_body_has_no_parameters(monkeypatch):
    sent = _capture(monkeypatch)
    _mgr().submit(name="t", language="en", category="UTILITY", body="No variables here.",
                  variables=[], examples=[])
    body_comp = next(c for c in sent["payload"]["components"] if c["type"] == "BODY")
    assert "example" not in body_comp        # a template with no {{n}} needs no example


def test_submit_declares_a_flow_button_in_a_buttons_component(monkeypatch):
    # A Flow button must be APPROVED as part of the template before any send can carry a flow action.
    # Meta's shape (verified Sep 2026): a BUTTONS component with a {"type":"FLOW"} button.
    sent = _capture(monkeypatch)
    _mgr().submit(name="book", language="en", category="MARKETING", body="Tap to book.",
                  variables=[], examples=[],
                  flow_button={"flow_id": "1234", "text": "Book now",
                               "flow_action": "navigate", "navigate_screen": "WELCOME"})
    btns = next(c for c in sent["payload"]["components"] if c["type"] == "BUTTONS")
    assert btns["buttons"] == [{"type": "FLOW", "text": "Book now", "flow_id": "1234",
                                "flow_action": "navigate", "navigate_screen": "WELCOME"}]


def test_submit_data_exchange_flow_omits_navigate_screen(monkeypatch):
    # navigate_screen is a navigate-only field; a data_exchange flow must not carry a screen id.
    sent = _capture(monkeypatch)
    _mgr().submit(name="dyn", language="en", category="UTILITY", body="Hi", variables=[], examples=[],
                  flow_button={"flow_name": "survey", "flow_action": "data_exchange",
                               "navigate_screen": "IGNORED"})
    btn = next(c for c in sent["payload"]["components"] if c["type"] == "BUTTONS")["buttons"][0]
    assert btn == {"type": "FLOW", "text": "Open", "flow_name": "survey",
                   "flow_action": "data_exchange"}


def test_submit_without_a_flow_button_sends_no_buttons_component(monkeypatch):
    sent = _capture(monkeypatch)
    _mgr().submit(name="plain", language="en", category="UTILITY", body="Hi {{1}}",
                  variables=["n"], examples=["Sam"])
    assert all(c["type"] != "BUTTONS" for c in sent["payload"]["components"])


def test_submit_builds_a_carousel_component(monkeypatch):
    # Meta's carousel-template creation shape (verified Sep 2026): a CAROUSEL component of cards, each
    # card a HEADER (format + example.header_handle), optional BODY, and a BUTTONS component.
    sent = _capture(monkeypatch)
    carousel = {"cards": [
        {"header_format": "IMAGE", "header_example_handle": "h0", "body": "Card {{1}}",
         "body_examples": ["Pizza"],
         "buttons": [{"type": "QUICK_REPLY", "text": "Order"},
                     {"type": "URL", "text": "Menu", "url": "https://x/{{1}}", "url_example": ["a"]}]},
        {"header_format": "IMAGE", "header_example_handle": "h1", "body": "Card {{1}}",
         "body_examples": ["Burger"],
         "buttons": [{"type": "QUICK_REPLY", "text": "Order"},
                     {"type": "URL", "text": "Menu", "url": "https://x/{{1}}", "url_example": ["b"]}]}]}
    _mgr().submit(name="menu", language="en", category="MARKETING", body="Offers", variables=[],
                  examples=[], carousel=carousel)
    car = next(c for c in sent["payload"]["components"] if c["type"] == "CAROUSEL")
    assert len(car["cards"]) == 2
    c0 = car["cards"][0]["components"]
    hdr = next(c for c in c0 if c["type"] == "HEADER")
    assert hdr == {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["h0"]}}
    body = next(c for c in c0 if c["type"] == "BODY")
    assert body["example"] == {"body_text": [["Pizza"]]}
    btns = next(c for c in c0 if c["type"] == "BUTTONS")["buttons"]
    assert btns[0] == {"type": "QUICK_REPLY", "text": "Order"}
    assert btns[1] == {"type": "URL", "text": "Menu", "url": "https://x/{{1}}", "example": ["a"]}


def test_paused_and_disabled_are_distinct_statuses_not_pending(monkeypatch):
    # The single failure a messaging product most needs to surface: a live template Meta has muted.
    for meta_status, ours in (("PAUSED", "paused"), ("DISABLED", "disabled"),
                              ("IN_REVIEW", "pending"), ("APPROVED", "approved")):
        _capture(monkeypatch, response=(200, json.dumps({
            "data": [{"status": meta_status, "language": "en"}]})))
        res = _mgr().status(provider_template_id="meta:1", name="t", language="en")
        assert res.status == ours, f"{meta_status} -> {res.status}, expected {ours}"


# ── messaging limit (read the phone number's ceiling + quality from Meta) ─────────
def test_meta_messaging_limit_reads_the_current_field_and_maps_the_tier(monkeypatch):
    seen = {}

    def fake_post_json(url, payload, headers):
        seen["url"] = url
        return (200, json.dumps({
            "whatsapp_business_manager_messaging_limit": "TIER_10K",
            "quality_rating": "GREEN", "display_phone_number": "+966500000000",
            "verified_name": "Acme", "name_status": "APPROVED", "id": "phone_1"}))

    monkeypatch.setattr(meta_mod, "post_json", fake_post_json)
    r = _mgr().messaging_limit(phone_number_id="phone_1")
    assert r.ok and r.tier == "TIER_10K" and r.limit == 10000
    assert r.quality_rating == "GREEN" and r.verified_name == "Acme"
    # the CURRENT field (not the deprecated messaging_limit_tier) is requested, on the phone number
    assert "whatsapp_business_manager_messaging_limit" in seen["url"] and "phone_1" in seen["url"]


def test_meta_messaging_limit_unlimited_and_unknown(monkeypatch):
    monkeypatch.setattr(meta_mod, "post_json",
                        lambda u, p, h: (200, json.dumps({"whatsapp_business_manager_messaging_limit": "TIER_UNLIMITED",
                                                          "quality_rating": "YELLOW"})))
    r = _mgr().messaging_limit(phone_number_id="p")
    assert r.ok and r.limit == -1 and r.quality_rating == "YELLOW"


def test_meta_messaging_limit_reports_an_http_error_without_faking_a_ceiling(monkeypatch):
    monkeypatch.setattr(meta_mod, "post_json",
                        lambda u, p, h: (400, json.dumps({"error": {"message": "bad token"}})))
    r = _mgr().messaging_limit(phone_number_id="p")
    assert r.ok is False and "bad token" in r.error and r.limit == 0


def test_meta_messaging_limit_needs_a_phone_number_id():
    assert _mgr().messaging_limit(phone_number_id="").ok is False


@pytest.mark.parametrize("tier,expected", [
    ("TIER_250", 250), ("TIER_1K", 1000), ("TIER_10K", 10000), ("TIER_100K", 100000),
    ("TIER_UNLIMITED", -1), ("TIER_50", 50), ("", 0), ("TIER_2K", 2000), ("garbage", 0),
])
def test_tier_to_int_is_generic(tier, expected):
    from app.adapters.whatsapp_templates.meta import _tier_to_int
    assert _tier_to_int(tier) == expected
