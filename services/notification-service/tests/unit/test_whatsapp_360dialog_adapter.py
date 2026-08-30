"""ThreeSixtyDialogWhatsApp — the 360dialog BSP sender. It is MetaWhatsApp's twin on the wire, so the
tests pin exactly the three things that are DIFFERENT and would silently misroute a send:

  * the endpoint is a flat `/messages` (no phone_number_id in the path — the key selects the channel),
  * auth is a `D360-API-KEY` header, never Authorization: Bearer,
  * the success body is still Meta-shaped, so messages[0].id (the wamid) is returned verbatim.

Plus the two contracts every provider shares: an accepted send returns the wamid; a non-2xx returns
"" so FailoverWhatsApp falls through to the next provider instead of reporting a phantom success.
"""
from __future__ import annotations

import json

import pytest

from app.adapters.whatsapp import threesixtydialog as d360_mod
from app.adapters.whatsapp.threesixtydialog import ThreeSixtyDialogWhatsApp
from app.ports.errors import ConfigError

_BASE = "https://waba-v2.360dialog.test"


def _capture(monkeypatch, response=(200, json.dumps({"messages": [{"id": "wamid.360d.1"}]}))):
    sent = {}

    def fake_post_json(url, payload, headers):
        sent["url"], sent["payload"], sent["headers"] = url, payload, headers
        return response

    monkeypatch.setattr(d360_mod, "post_json", fake_post_json)
    return sent


def _p(base=_BASE):
    return ThreeSixtyDialogWhatsApp(api_key="d360-secret", base_url=base)


def test_requires_an_api_key():
    with pytest.raises(ConfigError):
        ThreeSixtyDialogWhatsApp(api_key="")


def test_template_send_hits_flat_messages_with_the_d360_header(monkeypatch):
    sent = _capture(monkeypatch)
    wamid = _p().send(to="966500000000", template_name="order_ready", language="ar",
                      variables=["محمد", "مغسلة النخيل"])
    assert wamid == "wamid.360d.1"                      # the join key comes straight back
    assert sent["url"] == f"{_BASE}/messages"           # NO /{phone_number_id}/ segment
    assert sent["headers"]["D360-API-KEY"] == "d360-secret"
    assert "Authorization" not in sent["headers"]       # never a Bearer token
    p = sent["payload"]
    assert p["messaging_product"] == "whatsapp" and p["to"] == "966500000000"
    assert p["type"] == "template" and p["template"]["name"] == "order_ready"
    assert p["template"]["language"]["code"] == "ar"
    assert p["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "محمد"}, {"type": "text", "text": "مغسلة النخيل"}]


def test_interactive_send_wraps_the_object_and_carries_opaque_data(monkeypatch):
    sent = _capture(monkeypatch)
    interactive = {"type": "button", "body": {"text": "pick"}}
    _p().send_interactive(to="966500000000", interactive=interactive, biz_opaque_callback_data="send-42")
    assert sent["url"] == f"{_BASE}/messages"
    assert sent["payload"]["type"] == "interactive"
    assert sent["payload"]["interactive"] == interactive
    assert sent["payload"]["biz_opaque_callback_data"] == "send-42"


def test_default_base_is_the_real_360dialog_host(monkeypatch):
    sent = _capture(monkeypatch)
    ThreeSixtyDialogWhatsApp(api_key="k").send(to="1", template_name="t", language="en", variables=[])
    assert sent["url"] == "https://waba-v2.360dialog.io/messages"


def test_a_non_2xx_returns_empty_so_failover_falls_through(monkeypatch):
    _capture(monkeypatch, response=(401, json.dumps({"error": "bad key"})))
    assert _p().send(to="1", template_name="t", language="en", variables=[]) == ""


def test_an_accepted_but_unparseable_body_stays_truthy(monkeypatch):
    _capture(monkeypatch, response=(200, "not json"))
    assert _p().send(to="1", template_name="t", language="en", variables=[]) == "wamid:unparsed"


def test_failover_falls_from_a_dead_360dialog_to_the_next_provider(monkeypatch):
    # A single-provider concern proven at the chain level: "" from 360dialog must advance the chain.
    from app.adapters.failover import FailoverWhatsApp
    _capture(monkeypatch, response=(500, "boom"))

    class _Console:
        driver = "console"

        def send(self, to, template_name, language, variables, biz_opaque_callback_data=""):
            return "wamid:console"

    ok, provider, mid = FailoverWhatsApp([_p(), _Console()]).send(
        to="1", template_name="t", language="en", variables=[])
    assert ok and provider == "console" and mid == "wamid:console"
