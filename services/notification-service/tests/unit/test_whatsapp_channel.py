"""The WhatsApp channel — provider drivers + failover, mirroring the email/SMS channel (54.1)."""
from __future__ import annotations

from app.adapters.failover import FailoverWhatsApp
from app.adapters.whatsapp.console import ConsoleWhatsApp
from app.adapters.whatsapp.meta import MetaWhatsApp
from app.adapters.whatsapp.webhook import WebhookWhatsApp
from app.ports.whatsapp_provider import WhatsAppProvider


def test_the_drivers_satisfy_the_port():
    # EVERY driver, so adding a port method (e.g. send_text) and missing one driver is caught here —
    # runtime_checkable isinstance fails the moment a declared method is absent.
    assert isinstance(ConsoleWhatsApp(), WhatsAppProvider)
    assert isinstance(MetaWhatsApp("tok", "pnid"), WhatsAppProvider)
    assert isinstance(WebhookWhatsApp("https://relay.example.com/wa"), WhatsAppProvider)


def test_console_reports_success():
    # send now returns the provider message id ("" == failure); console yields a synthetic one.
    mid = ConsoleWhatsApp().send("+9665...", "otp", "en", ["123456"])
    assert mid and mid.startswith("console:")


def test_meta_requires_credentials():
    import pytest
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        MetaWhatsApp("", "")


class _P:
    def __init__(self, driver, ok):
        self.driver = driver
        self._mid = f"{driver}:wamid" if ok else ""   # "" == failure, non-empty == the wamid
        self.calls = []

    def send(self, to, template_name, language, variables, biz_opaque_callback_data="",
             flow_token="", flow_action_data=None, header_media_id="", button_payload=""):
        self.calls.append((to, template_name, language, tuple(variables)))
        return self._mid


def test_failover_tries_each_until_one_wins():
    down, up = _P("meta", False), _P("console", True)
    ok, provider, mid = FailoverWhatsApp([down, up]).send("+966", "otp", "en", ["1"])
    assert ok is True and provider == "console" and mid == "console:wamid"   # the wamid is carried up
    assert down.calls and up.calls               # tried meta, then console


def test_failover_reports_the_chain_as_its_driver():
    fo = FailoverWhatsApp([_P("meta", True), _P("console", True)])
    assert fo.driver == "meta,console"


def test_failover_all_down_returns_false():
    ok, _, mid = FailoverWhatsApp([_P("meta", False), _P("webhook", False)]).send("+966", "t", "en", [])
    assert ok is False and mid == ""


def test_a_raising_provider_is_skipped_not_fatal():
    class _Boom:
        driver = "meta"

        def send(self, *a, **k):
            raise RuntimeError("network")

    ok, provider, mid = FailoverWhatsApp([_Boom(), _P("console", True)]).send("+966", "t", "en", [])
    assert ok is True and provider == "console"


# ── Meta payload-shape tests (54.x): the envelope each send builds, without a real HTTP call ──
def _capture_meta(monkeypatch):
    """Patch the module-level post_json meta.py imported; capture (url, payload) and return a wamid."""
    import app.adapters.whatsapp.meta as meta
    calls = []
    def fake_post(url, payload, headers):
        calls.append((url, payload))
        return 200, '{"messages":[{"id":"wamid.TEST"}]}'
    monkeypatch.setattr(meta, "post_json", fake_post)
    return calls


def test_meta_send_text_builds_a_plain_text_envelope(monkeypatch):
    calls = _capture_meta(monkeypatch)
    mid = MetaWhatsApp("tok", "pnid").send_text("+96650", "hi there", biz_opaque_callback_data="cb-1")
    assert mid == "wamid.TEST"
    url, p = calls[-1]
    assert url.endswith("/pnid/messages")
    assert p["messaging_product"] == "whatsapp" and p["to"] == "+96650"
    assert p["type"] == "text" and p["text"] == {"body": "hi there"}
    assert p["biz_opaque_callback_data"] == "cb-1"


def test_meta_send_text_truncates_the_callback_data_to_512(monkeypatch):
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send_text("+96650", "hi", biz_opaque_callback_data="x" * 600)
    assert len(calls[-1][1]["biz_opaque_callback_data"]) == 512


def test_meta_send_text_omits_callback_data_when_absent(monkeypatch):
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send_text("+96650", "hi")
    assert "biz_opaque_callback_data" not in calls[-1][1]


def test_meta_template_and_interactive_envelope_shapes(monkeypatch):
    calls = _capture_meta(monkeypatch)
    wa = MetaWhatsApp("tok", "pnid")
    wa.send("+96650", "otp", "en", ["123456"])
    p = calls[-1][1]
    assert p["type"] == "template" and p["template"]["name"] == "otp"
    assert p["template"]["components"][0]["parameters"][0]["text"] == "123456"
    wa.send_interactive("+96650", {"type": "button", "body": {"text": "hi"}})
    p2 = calls[-1][1]
    assert p2["type"] == "interactive" and p2["interactive"] == {"type": "button", "body": {"text": "hi"}}


def test_meta_send_with_a_flow_token_adds_a_flow_button_component(monkeypatch):
    # A send against a template whose approved Flow button carries a per-send flow action. Meta's
    # shape (verified Sep 2026): a components[] "button"/sub_type "flow" carrying the flow_token.
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "book", "en", ["Ahmed"],
                                     flow_token="rate:vis_1", flow_action_data={"customer": "Ahmed"})
    comps = calls[-1][1]["template"]["components"]
    btn = next(c for c in comps if c.get("type") == "button")
    assert btn["sub_type"] == "flow" and btn["index"] == "0"
    action = btn["parameters"][0]["action"]
    assert action["flow_token"] == "rate:vis_1" and action["flow_action_data"] == {"customer": "Ahmed"}


def test_meta_send_flow_omits_action_data_when_empty(monkeypatch):
    # Meta: flow_action_data is optional but must be NON-EMPTY if present, so an empty dict is omitted.
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "book", "en", [], flow_token="t1", flow_action_data={})
    btn = next(c for c in calls[-1][1]["template"]["components"] if c.get("type") == "button")
    assert "flow_action_data" not in btn["parameters"][0]["action"]


def test_meta_send_with_header_media_id_adds_an_image_header_component(monkeypatch):
    # A single-message (non-carousel) campaign template with an image header. Same header shape the
    # carousel path builds per card: {"type":"header","parameters":[{"type":"image","image":{"id":…}}]}.
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "promo", "en", ["Ahmed"], header_media_id="MEDIA-9")
    comps = calls[-1][1]["template"]["components"]
    hdr = next(c for c in comps if c.get("type") == "header")
    assert hdr["parameters"][0] == {"type": "image", "image": {"id": "MEDIA-9"}}
    # header comes first, before the body, matching Meta's component order
    assert comps[0]["type"] == "header" and any(c["type"] == "body" for c in comps)


def test_meta_send_without_header_media_id_has_no_header_component(monkeypatch):
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "promo", "en", ["Ahmed"])
    comps = calls[-1][1]["template"].get("components", [])
    assert not any(c.get("type") == "header" for c in comps)


def test_meta_send_with_button_payload_adds_a_quick_reply_button_component(monkeypatch):
    # A single-message campaign template with a per-send quick-reply payload. Same button shape the
    # carousel path builds per card: {"type":"button","sub_type":"quick_reply","index":"0",...}.
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "promo", "en", ["Ahmed"],
                                     button_payload="book:svc_42")
    comps = calls[-1][1]["template"]["components"]
    btn = next(c for c in comps if c.get("type") == "button")
    assert btn["sub_type"] == "quick_reply" and btn["index"] == "0"
    assert btn["parameters"][0] == {"type": "payload", "payload": "book:svc_42"}


def test_meta_send_without_button_payload_has_no_button_component(monkeypatch):
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "promo", "en", ["Ahmed"])
    comps = calls[-1][1]["template"].get("components", [])
    assert not any(c.get("type") == "button" for c in comps)


def test_meta_send_combines_header_body_and_button(monkeypatch):
    # All three optional components on one ordinary send, in Meta's order: header, body, button.
    calls = _capture_meta(monkeypatch)
    MetaWhatsApp("tok", "pnid").send("+96650", "promo", "en", ["Ahmed"],
                                     header_media_id="MEDIA-1", button_payload="book:svc_1")
    types = [c["type"] for c in calls[-1][1]["template"]["components"]]
    assert types == ["header", "body", "button"]
