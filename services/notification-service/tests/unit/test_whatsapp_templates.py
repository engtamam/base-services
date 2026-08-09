"""The WhatsApp template registry — register/list/get, tenant-scoped, validated (54.2)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.whatsapp_templates import (APPROVED, CATEGORIES, PENDING, TemplateError,
                                         WhatsAppTemplateRegistry, WhatsAppTemplateStore)


def run(c):
    return asyncio.run(c)


def _reg():
    return WhatsAppTemplateRegistry(WhatsAppTemplateStore(MemoryDataStore()))


def test_register_starts_pending_and_stores_the_shape():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="otp", language="en", category="AUTHENTICATION",
                         body="Your code is {{1}}", variables=["code"]))
    assert t.status == PENDING and t.category == "AUTHENTICATION" and t.variables == ["code"]


def test_category_is_validated():
    reg = _reg()
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="PROMO"))


@pytest.mark.parametrize("cat", CATEGORIES)
def test_all_meta_categories_are_accepted(cat):
    reg = _reg()
    t = run(reg.register(tenant="acme", name="t", language="en", category=cat))
    assert t.category == cat


def test_tenant_name_language_are_required():
    reg = _reg()
    with pytest.raises(TemplateError):
        run(reg.register(tenant="", name="x", language="en", category="UTILITY"))


def test_re_registering_updates_in_place_not_duplicates():
    reg = _reg()
    a = run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY"))
    b = run(reg.register(tenant="acme", name="otp", language="en", category="AUTHENTICATION"))
    assert a.id == b.id                              # same identity (tenant,name,language)
    rows, _ = run(reg.list("acme"))
    assert len(rows) == 1 and rows[0]["category"] == "AUTHENTICATION"


def test_re_registering_identical_content_keeps_an_approved_template_approved():
    # "make sure this tenant has the library" is run over the whole set whenever ONE template is
    # added. If that moved every unchanged, already-approved template back to pending, every
    # business-initiated message would stop until each was re-reviewed. So a no-op re-register must
    # preserve the status (and the Meta id).
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY",
                     body="Your code is {{1}}", variables=["code"], examples=["123456"]))
    t = run(reg.get("acme", "otp", "en")); t.status = APPROVED
    t.provider_template_id = "meta:9"; run(st.save(t))
    # Byte-identical re-register.
    again = run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY",
                             body="Your code is {{1}}", variables=["code"], examples=["123456"]))
    assert again.status == APPROVED and again.provider_template_id == "meta:9"


def test_re_registering_changed_content_resets_to_pending():
    # An edit does need re-approval — Meta re-opens review on an edit, not only a rename.
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY",
                     body="Your code is {{1}}", variables=["code"]))
    t = run(reg.get("acme", "otp", "en")); t.status = APPROVED; run(st.save(t))
    again = run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY",
                             body="Your NEW code is {{1}}", variables=["code"]))   # body changed
    assert again.status == PENDING


def test_get_resolves_by_identity():
    reg = _reg()
    run(reg.register(tenant="acme", name="otp", language="ar", category="UTILITY"))
    assert run(reg.get("acme", "otp", "ar")) is not None
    assert run(reg.get("acme", "otp", "en")) is None     # different language
    assert run(reg.get("other", "otp", "ar")) is None    # different tenant


def test_list_is_tenant_scoped():
    reg = _reg()
    run(reg.register(tenant="acme", name="a", language="en", category="UTILITY"))
    run(reg.register(tenant="globex", name="b", language="en", category="UTILITY"))
    rows, _ = run(reg.list("acme"))
    assert len(rows) == 1 and rows[0]["name"] == "a"


def test_list_can_filter_by_status():
    reg = _reg()
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="a", language="en", category="UTILITY"))
    t = run(reg.get("acme", "a", "en")); t.status = APPROVED; run(st.save(t))
    approved, _ = run(reg.list("acme", status=APPROVED))
    pending, _ = run(reg.list("acme", status=PENDING))
    assert len(approved) == 1 and len(pending) == 0
