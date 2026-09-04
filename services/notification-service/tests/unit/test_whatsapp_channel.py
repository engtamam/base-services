"""The WhatsApp channel — provider drivers + failover, mirroring the email/SMS channel (54.1)."""
from __future__ import annotations

from app.adapters.failover import FailoverWhatsApp
from app.adapters.whatsapp.console import ConsoleWhatsApp
from app.adapters.whatsapp.meta import MetaWhatsApp
from app.adapters.whatsapp.threesixtydialog import ThreeSixtyDialogWhatsApp
from app.adapters.whatsapp.webhook import WebhookWhatsApp
from app.ports.whatsapp_provider import WhatsAppProvider


def test_the_drivers_satisfy_the_port():
    # EVERY driver, so adding a port method (e.g. send_text) and missing one driver is caught here —
    # runtime_checkable isinstance fails the moment a declared method is absent. (360dialog was the
    # one missed when send_text landed; this line is why it can't happen silently again.)
    assert isinstance(ConsoleWhatsApp(), WhatsAppProvider)
    assert isinstance(MetaWhatsApp("tok", "pnid"), WhatsAppProvider)
    assert isinstance(ThreeSixtyDialogWhatsApp("apikey"), WhatsAppProvider)
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

    def send(self, to, template_name, language, variables, biz_opaque_callback_data=""):
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
