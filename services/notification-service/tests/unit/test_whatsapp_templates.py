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


def test_register_stores_a_flow_button_and_needs_an_identifier():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="book", language="en", category="MARKETING",
                         body="Tap to book", flow_button={"flow_id": "1", "text": "Book"}))
    assert t.flow_button == {"flow_id": "1", "text": "Book"}
    with pytest.raises(TemplateError):   # a flow button with neither flow_id nor flow_name is refused
        run(reg.register(tenant="acme", name="bad", language="en", category="MARKETING",
                         flow_button={"text": "no id"}))


def test_changing_the_flow_button_resets_an_approved_template_to_pending():
    # The flow button is part of what Meta reviews — changing it must re-open review, like a body edit.
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="book", language="en", category="MARKETING",
                     body="Tap", flow_button={"flow_id": "1", "text": "Book"}))
    t = run(reg.get("acme", "book", "en")); t.status = APPROVED; run(st.save(t))
    same = run(reg.register(tenant="acme", name="book", language="en", category="MARKETING",
                            body="Tap", flow_button={"flow_id": "1", "text": "Book"}))
    assert same.status == APPROVED            # identical flow button is a no-op, stays approved
    changed = run(reg.register(tenant="acme", name="book", language="en", category="MARKETING",
                               body="Tap", flow_button={"flow_id": "2", "text": "Book"}))
    assert changed.status == PENDING          # different flow id → re-review


def test_register_stores_and_trims_a_footer():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                         body="Sale", footer="  Reply STOP to opt out  "))
    assert t.footer == "Reply STOP to opt out"     # trimmed, stored, and round-trips
    assert run(reg.get("acme", "promo", "en")).footer == "Reply STOP to opt out"


def test_footer_rejects_variables_and_over_length():
    reg = _reg()
    with pytest.raises(TemplateError):             # Meta allows no variables in a footer
        run(reg.register(tenant="acme", name="a", language="en", category="MARKETING",
                         body="x", footer="Hi {{1}}"))
    with pytest.raises(TemplateError):             # 60-char cap
        run(reg.register(tenant="acme", name="b", language="en", category="MARKETING",
                         body="x", footer="z" * 61))
    # exactly 60 is fine (boundary)
    t = run(reg.register(tenant="acme", name="c", language="en", category="MARKETING",
                         body="x", footer="z" * 60))
    assert len(t.footer) == 60


def test_changing_the_footer_resets_an_approved_template_to_pending():
    # The footer is part of what Meta reviews — changing it must re-open review, like a body edit.
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                     body="Sale", footer="Old footer"))
    t = run(reg.get("acme", "promo", "en")); t.status = APPROVED; run(st.save(t))
    same = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                            body="Sale", footer="Old footer"))
    assert same.status == APPROVED                 # identical footer is a no-op, stays approved
    changed = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                               body="Sale", footer="New footer"))
    assert changed.status == PENDING               # different footer → re-review


def test_register_stores_a_flat_header_and_button():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                         body="Sale", header={"format": "image", "example_handle": "H1"},
                         button={"type": "quick_reply", "text": "Shop"}))
    assert t.header == {"format": "IMAGE", "example_handle": "H1"}     # normalised (upper) + stored
    assert t.button == {"type": "QUICK_REPLY", "text": "Shop"}
    got = run(reg.get("acme", "promo", "en"))
    assert got.header == t.header and got.button == t.button           # round-trips


def test_flat_header_needs_a_valid_format_and_handle():
    reg = _reg()
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="a", language="en", category="MARKETING",
                         header={"format": "AUDIO", "example_handle": "h"}))
    with pytest.raises(TemplateError):     # missing handle
        run(reg.register(tenant="acme", name="b", language="en", category="MARKETING",
                         header={"format": "IMAGE"}))


def test_flat_button_validates_type_text_and_url():
    reg = _reg()
    with pytest.raises(TemplateError):     # bad type
        run(reg.register(tenant="acme", name="a", language="en", category="MARKETING",
                         button={"type": "FLOW", "text": "x"}))
    with pytest.raises(TemplateError):     # label over 25
        run(reg.register(tenant="acme", name="b", language="en", category="MARKETING",
                         button={"type": "QUICK_REPLY", "text": "z" * 26}))
    with pytest.raises(TemplateError):     # URL button with no url
        run(reg.register(tenant="acme", name="c", language="en", category="MARKETING",
                         button={"type": "URL", "text": "Open"}))


def test_flat_components_are_mutually_exclusive_with_carousel_and_flow_button():
    reg = _reg()
    car = {"cards": [
        {"header_format": "IMAGE", "header_example_handle": "h0",
         "buttons": [{"type": "QUICK_REPLY", "text": "Book"}]},
        {"header_format": "IMAGE", "header_example_handle": "h1",
         "buttons": [{"type": "QUICK_REPLY", "text": "Book"}]}]}
    with pytest.raises(TemplateError):     # carousel + flat header
        run(reg.register(tenant="acme", name="a", language="en", category="MARKETING",
                         carousel=car, header={"format": "IMAGE", "example_handle": "h"}))
    with pytest.raises(TemplateError):     # flow_button + flat button = two BUTTONS
        run(reg.register(tenant="acme", name="b", language="en", category="MARKETING",
                         flow_button={"flow_id": "1", "text": "Book"},
                         button={"type": "QUICK_REPLY", "text": "Shop"}))
    # header alongside a flow_button is allowed (HEADER + one BUTTONS — distinct components)
    t = run(reg.register(tenant="acme", name="c", language="en", category="MARKETING",
                         header={"format": "IMAGE", "example_handle": "h"},
                         flow_button={"flow_id": "1", "text": "Book"}))
    assert t.header and t.flow_button


def test_changing_a_flat_header_resets_an_approved_template_to_pending():
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                     body="Sale", header={"format": "IMAGE", "example_handle": "H1"}))
    t = run(reg.get("acme", "promo", "en")); t.status = APPROVED; run(st.save(t))
    same = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                            body="Sale", header={"format": "IMAGE", "example_handle": "H1"}))
    assert same.status == APPROVED         # identical → no-op
    changed = run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING",
                               body="Sale", header={"format": "IMAGE", "example_handle": "H2"}))
    assert changed.status == PENDING       # new handle → re-review


def _carousel(n=2, header="IMAGE", body=None):
    card = lambda i: {   # noqa: E731 — terse test factory
        "header_format": header, "header_example_handle": f"h{i}",
        "buttons": [{"type": "QUICK_REPLY", "text": "Book"}],
        **({"body": body} if body else {})}
    return {"cards": [card(i) for i in range(n)]}


def test_register_stores_and_normalises_a_carousel():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="menu", language="en", category="MARKETING",
                         body="Our offers", carousel=_carousel(3)))
    assert len(t.carousel["cards"]) == 3
    assert t.carousel["cards"][0]["header_format"] == "IMAGE"
    assert t.carousel["cards"][0]["buttons"] == [{"type": "QUICK_REPLY", "text": "Book"}]


def test_carousel_card_count_is_bounded():
    reg = _reg()
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="MARKETING",
                         carousel=_carousel(1)))                     # <2
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="MARKETING",
                         carousel=_carousel(11)))                    # >10


def test_carousel_cards_must_share_the_same_shape():
    reg = _reg()
    mixed = {"cards": [
        {"header_format": "IMAGE", "header_example_handle": "h0",
         "buttons": [{"type": "QUICK_REPLY", "text": "A"}]},
        {"header_format": "VIDEO", "header_example_handle": "h1",   # different header format
         "buttons": [{"type": "QUICK_REPLY", "text": "A"}]}]}
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="MARKETING", carousel=mixed))


def test_carousel_card_requires_a_header_handle_and_a_button():
    reg = _reg()
    no_handle = {"cards": [{"header_format": "IMAGE", "buttons": [{"type": "QUICK_REPLY", "text": "A"}]}] * 2}
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="MARKETING", carousel=no_handle))


def test_url_carousel_button_needs_a_url():
    reg = _reg()
    bad = {"cards": [{"header_format": "IMAGE", "header_example_handle": "h",
                      "buttons": [{"type": "URL", "text": "Open"}]}] * 2}   # no url
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="x", language="en", category="MARKETING", carousel=bad))


def test_changing_the_carousel_resets_an_approved_template_to_pending():
    st = WhatsAppTemplateStore(MemoryDataStore())
    reg = WhatsAppTemplateRegistry(st)
    run(reg.register(tenant="acme", name="menu", language="en", category="MARKETING",
                     carousel=_carousel(2)))
    t = run(reg.get("acme", "menu", "en")); t.status = APPROVED; run(st.save(t))
    same = run(reg.register(tenant="acme", name="menu", language="en", category="MARKETING",
                            carousel=_carousel(2)))
    assert same.status == APPROVED                                   # identical → stays approved
    changed = run(reg.register(tenant="acme", name="menu", language="en", category="MARKETING",
                               carousel=_carousel(3)))
    assert changed.status == PENDING                                 # card count changed → re-review


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


# ── notification-005: a MARKETING WhatsApp template must be mapped to a consent purpose ─────────────
def test_unmapped_marketing_whatsapp_template_is_refused():
    reg = _reg()
    with pytest.raises(TemplateError):
        run(reg.register(tenant="acme", name="promo_sale", language="en", category="MARKETING",
                         body="Sale!", consent_purposes={}))          # unmapped → refused


def test_mapped_marketing_whatsapp_template_is_allowed():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="promo_sale", language="en", category="MARKETING",
                         body="Sale!", consent_purposes={"promo_sale": "marketing"}))
    assert t.category == "MARKETING"


def test_marketing_check_is_skipped_when_no_consent_map_is_wired():
    # consent_purposes=None (dev/raw mode) keeps prior behaviour — no enforcement.
    reg = _reg()
    t = run(reg.register(tenant="acme", name="promo_sale", language="en", category="MARKETING",
                         body="Sale!"))
    assert t.category == "MARKETING"
