"""MetaFlowManager — the driver that talks to the Graph Flows API. The one question: does it send
Meta a request Meta will accept, and read the response an operator/provisioner can act on?

Verified shapes (Sep 2026): create POST /{waba}/flows {name,categories,flow_json|clone_flow_id,
publish?} → {id,success,validation_errors}; publish POST /{flow_id}/publish; list GET /{waba}/flows
→ {data:[{id,name,status,categories}]}.
"""
from __future__ import annotations

import json

from app.adapters.whatsapp_flows import meta as meta_mod
from app.adapters.whatsapp_flows.meta import MetaFlowManager

_BASE = "https://graph.facebook.test/v20.0"


def _mgr():
    return MetaFlowManager(access_token="tok", waba_id="WABA1", base_url=_BASE)


def _post(monkeypatch, response):
    sent = {}

    def fake_post_json(url, payload, headers):
        sent["url"], sent["payload"] = url, payload
        return response

    monkeypatch.setattr(meta_mod, "post_json", fake_post_json)
    return sent


def test_create_from_json_shape(monkeypatch):
    sent = _post(monkeypatch, (200, json.dumps({"id": "flow:1", "success": True})))
    r = _mgr().create(name="rating", categories=["SURVEY"], flow_json='{"x":1}', publish=True)
    assert r.ok and r.flow_id == "flow:1"
    assert sent["url"] == f"{_BASE}/WABA1/flows"
    assert sent["payload"] == {"name": "rating", "categories": ["SURVEY"],
                               "flow_json": '{"x":1}', "publish": True}


def test_create_clone_shape(monkeypatch):
    sent = _post(monkeypatch, (200, json.dumps({"id": "flow:2", "success": True})))
    _mgr().create(name="rating", categories=["SURVEY"], clone_flow_id="master:9")
    assert sent["payload"]["clone_flow_id"] == "master:9" and "flow_json" not in sent["payload"]
    assert "publish" not in sent["payload"]   # omitted when False


def test_create_failure_carries_reason_and_validation_errors(monkeypatch):
    _post(monkeypatch, (400, json.dumps({"error": {"message": "bad flow_json"},
                                         "validation_errors": [{"message": "screen missing"}]})))
    r = _mgr().create(name="r", categories=["SURVEY"], flow_json="{}")
    assert r.ok is False and "bad flow_json" in r.reason and r.validation_errors == ("screen missing",)


def test_publish_shape(monkeypatch):
    sent = _post(monkeypatch, (200, json.dumps({"success": True})))
    r = _mgr().publish(flow_id="flow:1")
    assert r.ok and r.status == "PUBLISHED" and sent["url"] == f"{_BASE}/flow:1/publish"


def test_list_flows_maps_rows(monkeypatch):
    def fake_get_json(url, params=None, headers=None):
        return (200, json.dumps({"data": [
            {"id": "f1", "name": "rating", "status": "PUBLISHED", "categories": ["SURVEY"]},
            {"id": "f2", "name": "draft_one", "status": "DRAFT", "categories": ["OTHER"]}]}))

    monkeypatch.setattr(meta_mod, "get_json", fake_get_json)
    rows = _mgr().list_flows()
    assert {r.name for r in rows} == {"rating", "draft_one"}
    r0 = next(r for r in rows if r.name == "rating")
    assert r0.id == "f1" and r0.status == "PUBLISHED" and r0.categories == ("SURVEY",)


def test_list_flows_returns_empty_on_http_error_never_guesses(monkeypatch):
    monkeypatch.setattr(meta_mod, "get_json", lambda u, params=None, headers=None: (500, "boom"))
    assert _mgr().list_flows() == []
