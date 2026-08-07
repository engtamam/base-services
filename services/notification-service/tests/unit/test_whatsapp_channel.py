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
    assert ConsoleWhatsApp().send("+9665...", "otp", "en", ["123456"]) is True


def test_meta_requires_credentials():
    import pytest
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        MetaWhatsApp("", "")


class _P:
    def __init__(self, driver, ok):
        self.driver = driver
        self._ok = ok
        self.calls = []

    def send(self, to, template_name, language, variables):
        self.calls.append((to, template_name, language, tuple(variables)))
        return self._ok


def test_failover_tries_each_until_one_wins():
    down, up = _P("meta", False), _P("console", True)
    ok, provider = FailoverWhatsApp([down, up]).send("+966", "otp", "en", ["1"])
    assert ok is True and provider == "console"
    assert down.calls and up.calls               # tried meta, then console


def test_failover_reports_the_chain_as_its_driver():
    fo = FailoverWhatsApp([_P("meta", True), _P("console", True)])
    assert fo.driver == "meta,console"


def test_failover_all_down_returns_false():
    ok, _ = FailoverWhatsApp([_P("meta", False), _P("webhook", False)]).send("+966", "t", "en", [])
    assert ok is False


def test_a_raising_provider_is_skipped_not_fatal():
    class _Boom:
        driver = "meta"

        def send(self, *a):
            raise RuntimeError("network")

    ok, provider = FailoverWhatsApp([_Boom(), _P("console", True)]).send("+966", "t", "en", [])
    assert ok is True and provider == "console"
