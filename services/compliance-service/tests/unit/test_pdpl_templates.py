"""PDPL template pack — the well-known documents, bilingual rich blocks, and it becomes a real
publishable legal page. The backend hands over the blocks + the tenant's values UNCHANGED (the UI's
block renderer fills the {{TOKEN}}s and picks the language); its job is to refuse a document whose
placeholders are not all supplied, so a page with a live {{HOLE}} can never be published."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core import pdpl_templates
from app.core.legal_pages import LegalPageService
from app.core.pdpl_templates import TemplateError, render
from app.core.stores import LegalPageStore


def run(c):
    return asyncio.run(c)


def _all(tid):
    return {p: f"«{p}»" for t in pdpl_templates.list_templates() if t["id"] == tid for p in t["placeholders"]}


def test_the_pack_is_the_well_known_declared_documents():
    """The pack holds the recognised data-protection documents any site declares — Privacy, Cookies,
    Data Subject Rights, RoPA, DPIA, and the DPA pair — and NOT the internal-only policies or the
    operational logs/forms."""
    ids = {t["id"] for t in pdpl_templates.list_templates()}
    assert ids == {"doc01_privacy_policy", "doc03_data_subject_rights", "doc04_ropa", "doc10_dpia",
                   "doc12_cookies", "doc13_processing_agreement", "doc14_processor_dpa"}
    assert not (ids & {"doc08_governance", "doc11_security_of_processing",
                       "reg16_dsar_log", "form08_dpo_appointment"})


def test_render_returns_bilingual_title_rich_blocks_and_the_values():
    r = render("doc01_privacy_policy", _all("doc01_privacy_policy"))
    assert r["title"]["en"] == "Privacy Policy"
    assert r["title"]["ar"] == "سياسة الخصوصية"                # genuinely translated
    assert r["blocks"]                                          # rich blocks passed through, not flattened
    assert r["slug"] == "privacy-policy"
    # every placeholder the document uses is carried in values, so the renderer can fill every slot
    assert set(r["values"]) >= set(_all("doc01_privacy_policy"))


def test_placeholders_are_scanned_from_text_so_none_are_missed():
    """The source header under-declares slots; we scan the text. With the scanned set supplied there
    are NO missing placeholders — nothing the renderer would leave as a live {{HOLE}}."""
    for t in pdpl_templates.list_templates():
        r = render(t["id"], {p: "X" for p in t["placeholders"]}, strict=True)
        assert r["missing"] == []


def test_a_missing_placeholder_is_refused_not_guessed():
    vals = _all("doc12_cookies")
    vals.pop(next(iter(vals)))                                  # drop one required value
    with pytest.raises(TemplateError):
        render("doc12_cookies", vals, strict=True)
    out = render("doc12_cookies", vals, strict=False)          # non-strict reports instead of raising
    assert out["missing"]


def test_an_unknown_template_is_refused():
    with pytest.raises(TemplateError):
        render("doc99_not_real", {})


def test_render_then_publish_produces_a_versioned_gated_page_with_blocks():
    ds = MemoryDataStore()
    legal = LegalPageService(store=LegalPageStore(ds), clock=ManualClock(1_000.0))
    r = render("doc13_processing_agreement", _all("doc13_processing_agreement"))
    content = {"title": r["title"], "blocks": r["blocks"], "values": r["values"]}
    run(legal.draft(tenant="t", doc_id=r["slug"], version="1.0", content=content,
                    requires_acceptance=True, default_locale="ar"))
    run(legal.publish(tenant="t", doc_id=r["slug"], version="1.0"))
    view = run(legal.current_view(tenant="t", doc_id=r["slug"], locale="ar"))
    assert view["version"] == "1.0" and view["requires_acceptance"] is True
    assert view["blocks"] and view["values"]                   # the page carries its rich blocks + values
