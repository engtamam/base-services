"""Help generated from what the product declares about itself.

The behaviour that matters: the capability the platform enforces on a route becomes the capability
that governs whether the help is visible, and nothing here knows any particular service's name.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.knowledge_source.catalog import CatalogKnowledgeSource
from app.ports.errors import ConfigError

CATALOG = {
    "services": [
        {"service": "billing-service", "actions": [
            {"id": "billing.view_invoices", "label": "View my invoices", "method": "GET",
             "path": "/api/v1/invoices", "capability": "billing.view", "group": "Billing"},
            {"id": "billing.pay", "label": "Pay an invoice", "method": "POST",
             "path": "/api/v1/invoices/{id}/pay", "capability": "billing.manage", "group": "Billing"},
        ]},
        {"service": "acme-widgets", "actions": [      # an adopter's own service, not ours
            {"id": "widgets.create", "label": "Create a widget", "method": "POST",
             "path": "/api/v1/widgets", "capability": "widgets.manage"},
        ]},
    ],
    "unreachable": [],
}


def run(c):
    return asyncio.run(c)


def _src(payload=CATALOG, status=200, **kw):
    async def transport(url, headers):
        return status, payload
    return CatalogKnowledgeSource("http://gw/api/v1/app/catalog", "tok", transport=transport, **kw)


def test_every_declared_action_becomes_an_entry():
    snap = run(_src().load())
    assert {e.id for e in snap.entries} == {
        "action.billing.view_invoices", "action.billing.pay", "action.widgets.create"}


def test_an_adopters_own_service_appears_with_no_special_casing():
    """Nothing here knows any service by name — a service appears because it declared itself."""
    snap = run(_src().load())
    widget = next(e for e in snap.entries if e.id == "action.widgets.create")
    assert widget.metadata["service"] == "acme-widgets"


def test_the_routes_capability_becomes_the_entrys_requires():
    # One source, both meanings: what the platform enforces is what governs visibility of the help.
    snap = run(_src().load())
    pay = next(e for e in snap.entries if e.id == "action.billing.pay")
    assert pay.requires == "billing.manage"
    assert not pay.visible_to(frozenset())
    assert pay.visible_to(frozenset({"billing.manage"}))


def test_deep_link_details_are_carried_for_the_answer_layer():
    snap = run(_src().load())
    pay = next(e for e in snap.entries if e.id == "action.billing.pay")
    assert pay.metadata["method"] == "POST"
    assert pay.metadata["path"] == "/api/v1/invoices/{id}/pay"


def test_generated_entries_are_marked_so_they_never_overwrite_authored_ones():
    snap = run(_src().load())
    assert all(e.source == "catalog" for e in snap.entries)
    assert all(e.id.startswith("action.") for e in snap.entries), "namespaced against collision"


def test_entries_are_actions():
    assert all(e.kind == "action" for e in run(_src().load()).entries)


def test_an_action_without_a_label_is_skipped_not_rendered_as_its_id():
    # A raw id in help text reads as internals leaking into the product.
    payload = {"services": [{"service": "s", "actions": [
        {"id": "x.y", "label": "", "capability": "c"},
        {"id": "ok.z", "label": "Fine", "capability": "c"}]}]}
    assert {e.id for e in run(_src(payload).load()).entries} == {"action.ok.z"}


def test_an_unreachable_catalog_refuses_loudly():
    async def boom(url, headers):
        raise OSError("connection refused")
    src = CatalogKnowledgeSource("http://gw/x", "t", transport=boom)
    with pytest.raises(ConfigError, match="unreachable"):
        run(src.load())


def test_a_non_200_refuses_rather_than_serving_an_empty_catalog():
    # Empty and unreachable produce the same silence; only one is a deployment mistake.
    with pytest.raises(ConfigError, match="HTTP 503"):
        run(_src(status=503).load())


def test_an_empty_url_is_a_config_error():
    with pytest.raises(ConfigError):
        CatalogKnowledgeSource("")


def test_identical_catalogs_produce_the_same_generation():
    assert run(_src().load()).generation == run(_src().load()).generation


def test_a_changed_label_changes_the_generation():
    changed = {"services": [{"service": "billing-service", "actions": [
        {"id": "billing.pay", "label": "RENAMED", "capability": "billing.manage"}]}]}
    assert run(_src().load()).generation != run(_src(changed).load()).generation


def test_the_prefix_is_configurable_so_ids_cannot_be_forced_to_collide():
    snap = run(_src(prefix="cmd").load())
    assert all(e.id.startswith("cmd.") for e in snap.entries)
