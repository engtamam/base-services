"""POST/GET/DELETE /internal/assistant/connections — isolated per-tenant AI lines.

Token-authed, tenant-scoped, and the credential is WRITE-ONLY: no read route returns it.
(invoke() is covered at the core level with a fake caller — it needs a live model endpoint.)
"""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, PII_SALT="pepper",
                      SECRETS_KEK="0" * 64, ASSISTANT_CONN_REQUIRE_HTTPS="false",
                      ASSISTANT_CONN_ALLOW_PRIVATE_IPS="true")
    get_settings.cache_clear()
    from app.main import create_app
    c = TestClient(create_app(), raise_server_exceptions=False)
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _tok():
    from app.config import get_settings
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_requires_the_internal_token():
    c = TestClient(create_app_noenv())
    assert c.post("/internal/assistant/connections",
                  json={"tenant": "t1", "name": "a", "url": "http://127.0.0.1/v1"}).status_code == 401


def create_app_noenv():
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    return create_app()


def test_create_list_get_delete_roundtrip(client):
    r = client.post("/internal/assistant/connections", headers=_tok(), json={
        "tenant": "t1", "name": "support", "url": "http://127.0.0.1/v1", "shape": "chat", "model": "gpt",
        "auth": "api_key", "system_prompt": "be helpful", "input_price_micros": 100,
        "output_price_micros": 300, "credential": "sk-secret-key"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["name"] == "support" and "credential" not in r.json()   # never echoed

    lst = client.get("/internal/assistant/connections?tenant=t1", headers=_tok()).json()
    assert len(lst["connections"]) == 1 and lst["connections"][0]["id"] == cid

    got = client.get(f"/internal/assistant/connections/{cid}?tenant=t1", headers=_tok()).json()
    assert got["input_price_micros"] == 100 and "credential" not in got and "sk-secret" not in str(got)

    dele = client.delete(f"/internal/assistant/connections/{cid}?tenant=t1", headers=_tok()).json()
    assert dele["deleted"] is True
    assert client.get(f"/internal/assistant/connections/{cid}?tenant=t1", headers=_tok()).status_code == 404


def test_a_bad_shape_is_a_clear_422(client):
    r = client.post("/internal/assistant/connections", headers=_tok(),
                    json={"tenant": "t1", "name": "a", "url": "http://127.0.0.1/v1", "shape": "stream"})
    assert r.status_code == 422


def test_usage_starts_empty_for_a_new_connection(client):
    r = client.post("/internal/assistant/connections", headers=_tok(),
                    json={"tenant": "t1", "name": "a", "url": "http://127.0.0.1/v1"})
    cid = r.json()["id"]
    u = client.get(f"/internal/assistant/connections/{cid}/usage?tenant=t1", headers=_tok()).json()
    assert u["calls"] == 0 and u["cost_micros"] == 0


def test_tenant_isolation_on_get(client):
    r = client.post("/internal/assistant/connections", headers=_tok(),
                    json={"tenant": "t1", "name": "a", "url": "http://127.0.0.1/v1"})
    cid = r.json()["id"]
    # another tenant cannot fetch t1's connection
    assert client.get(f"/internal/assistant/connections/{cid}?tenant=t2", headers=_tok()).status_code == 404


# ── tool declarations (opt-in per connection) + the update route ─────────────

_TOOLS = [{"name": "book", "description": "book a slot",
           "parameters": {"type": "object", "properties": {"day": {"type": "string"}}}}]


def test_create_with_tools_roundtrips(client):
    r = client.post("/internal/assistant/connections", headers=_tok(), json={
        "tenant": "t1", "name": "booker", "url": "http://127.0.0.1/v1", "tools": _TOOLS})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["tools"][0]["name"] == "book"
    got = client.get(f"/internal/assistant/connections/{cid}?tenant=t1", headers=_tok()).json()
    assert got["tools"][0]["name"] == "book"


def test_default_connection_has_no_tools(client):
    r = client.post("/internal/assistant/connections", headers=_tok(),
                    json={"tenant": "t1", "name": "plain", "url": "http://127.0.0.1/v1"})
    assert r.json()["tools"] == []          # backward compatible: text-only by default


def test_a_bad_tool_is_a_422(client):
    r = client.post("/internal/assistant/connections", headers=_tok(),
                    json={"tenant": "t1", "name": "x", "url": "http://127.0.0.1/v1",
                          "tools": [{"description": "no name"}]})
    assert r.status_code == 422


def test_update_changes_tools_and_prompt(client):
    cid = client.post("/internal/assistant/connections", headers=_tok(),
                      json={"tenant": "t1", "name": "x", "url": "http://127.0.0.1/v1"}).json()["id"]
    r = client.put(f"/internal/assistant/connections/{cid}", headers=_tok(),
                   json={"tenant": "t1", "tools": _TOOLS, "system_prompt": "be brief"})
    assert r.status_code == 200, r.text
    assert r.json()["tools"][0]["name"] == "book" and r.json()["system_prompt"] == "be brief"


def test_update_a_missing_connection_is_404(client):
    r = client.put("/internal/assistant/connections/nope", headers=_tok(),
                   json={"tenant": "t1", "tools": _TOOLS})
    assert r.status_code == 404


def test_update_tenant_isolation(client):
    cid = client.post("/internal/assistant/connections", headers=_tok(),
                      json={"tenant": "t1", "name": "x", "url": "http://127.0.0.1/v1"}).json()["id"]
    # another tenant cannot update t1's connection
    r = client.put(f"/internal/assistant/connections/{cid}", headers=_tok(),
                   json={"tenant": "t2", "system_prompt": "hijack"})
    assert r.status_code == 404
