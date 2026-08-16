"""The WhatsApp channel — provider drivers + failover, mirroring the email/SMS channel (54.1)."""
from __future__ import annotations

from app.adapters.failover import FailoverWhatsApp
from app.adapters.whatsapp.console import ConsoleWhatsApp
from app.adapters.whatsapp.meta import MetaWhatsApp
from app.adapters.whatsapp.webhook import WebhookWhatsApp
from app.ports.whatsapp_provider import WhatsAppProvider


def test_the_drivers_satisfy_the_port():
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
