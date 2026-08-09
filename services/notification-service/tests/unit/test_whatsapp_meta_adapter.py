"""MetaTemplateManager — the driver that talks to Graph. The one question: does it send Meta a
request Meta will accept, and read back a status an operator can act on?

The blocking failure this guards: Meta refuses any parameterised template that arrives without an
example value per parameter ("Upon template creation, you must include an example value for each
parameter"). Every real template has a placeholder, so a submit with no example burned the template
NAME — which cannot be reused after approval — on a 400 discovered hours later.
"""
from __future__ import annotations

import json

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


def test_paused_and_disabled_are_distinct_statuses_not_pending(monkeypatch):
    # The single failure a messaging product most needs to surface: a live template Meta has muted.
    for meta_status, ours in (("PAUSED", "paused"), ("DISABLED", "disabled"),
                              ("IN_REVIEW", "pending"), ("APPROVED", "approved")):
        _capture(monkeypatch, response=(200, json.dumps({
            "data": [{"status": meta_status, "language": "en"}]})))
        res = _mgr().status(provider_template_id="meta:1", name="t", language="en")
        assert res.status == ours, f"{meta_status} -> {res.status}, expected {ours}"
