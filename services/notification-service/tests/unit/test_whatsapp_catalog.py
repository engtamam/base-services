"""MetaCatalogManager — the Graph calls for a WABA's catalog lifecycle. The one question per method:
does it send Meta the right endpoint + shape, and read the answer an operator can act on?"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.whatsapp_catalog import meta as meta_mod
from app.adapters.whatsapp_catalog.meta import MetaCatalogManager
from app.adapters.whatsapp_catalog.console import ConsoleCatalogManager

_BASE = "https://graph.facebook.test/v26.0"


def run(c):
    return asyncio.run(c)


def _mgr(business="BIZ1", waba="WABA1"):
    return MetaCatalogManager("tok", business, waba, _BASE)


def _capture(monkeypatch, post=(200, '{"id":"cat_1"}'), get=(200, '{"id":"cat_1","name":"Shop","product_count":3}')):
    sent = {}

    def fake_post(url, payload, headers):
        sent["post_url"], sent["post_payload"], sent["headers"] = url, payload, headers
        return post

    def fake_get(url, params=None, headers=None):
        sent["get_url"], sent["get_params"] = url, params
        return get

    monkeypatch.setattr(meta_mod, "post_json", fake_post)
    monkeypatch.setattr(meta_mod, "get_json", fake_get)
    return sent


def test_create_catalog_posts_to_the_owning_business(monkeypatch):
    sent = _capture(monkeypatch)
    res = run(_mgr().create_catalog(name="Shop"))
    assert res.ok and res.catalog_id == "cat_1"
    assert sent["post_url"] == f"{_BASE}/BIZ1/owned_product_catalogs"
    assert sent["post_payload"] == {"name": "Shop"}
    assert sent["headers"].get("Authorization") == "Bearer tok"


def test_create_catalog_without_a_business_id_fails_cleanly(monkeypatch):
    _capture(monkeypatch)
    res = run(_mgr(business="").create_catalog(name="Shop"))
    assert res.ok is False and "business" in res.error


def test_create_catalog_surfaces_a_meta_error(monkeypatch):
    _capture(monkeypatch, post=(400, '{"error":{"message":"bad name"}}'))
    res = run(_mgr().create_catalog(name="x"))
    assert res.ok is False and res.error == "bad name"


def test_connect_catalog_posts_to_the_waba(monkeypatch):
    sent = _capture(monkeypatch, post=(200, "{}"))
    res = run(_mgr().connect_catalog(catalog_id="cat_9"))
    assert res.ok and res.catalog_id == "cat_9"
    assert sent["post_url"] == f"{_BASE}/WABA1/product_catalogs"
    assert sent["post_payload"] == {"catalog_id": "cat_9"}


def test_list_catalogs_reads_the_wabas_catalogs(monkeypatch):
    _capture(monkeypatch, get=(200, '{"data":[{"id":"c1","name":"A"},{"id":"c2","name":"B"}]}'))
    rows = run(_mgr().list_catalogs())
    assert rows == [{"id": "c1", "name": "A"}, {"id": "c2", "name": "B"}]


def test_set_commerce_settings_posts_to_the_phone_number(monkeypatch):
    sent = _capture(monkeypatch, post=(200, "{}"))
    res = run(_mgr().set_commerce_settings(phone_number_id="PN1", is_cart_enabled=True,
                                           is_catalog_visible=False))
    assert res.ok
    assert sent["post_url"] == f"{_BASE}/PN1/whatsapp_commerce_settings"
    assert sent["post_payload"] == {"is_cart_enabled": True, "is_catalog_visible": False}


def test_a_meta_credential_is_required():
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        MetaCatalogManager("", "BIZ", "WABA", _BASE)


def test_console_manager_is_a_working_noop():
    m = ConsoleCatalogManager()
    created = run(m.create_catalog(name="Shop"))
    assert created.ok and created.catalog_id.startswith("catalog:")
    assert run(m.connect_catalog(catalog_id=created.catalog_id)).ok
    assert run(m.set_commerce_settings(phone_number_id="p", is_cart_enabled=True,
                                       is_catalog_visible=True)).ok
    assert run(m.list_catalogs()) == []
