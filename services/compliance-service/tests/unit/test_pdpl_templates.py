"""PDPL template pack — fill-in-the-blank, bilingual, and it becomes a real publishable legal page.
The one thing that must never happen: a policy published with a literal {{PLACEHOLDER}} in it."""
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
    Data Subject Rights, RoPA, DPIA, and the DPA pair — and NOT the internal-only policies
    (governance, security-of-processing…) or the operational logs/forms."""
    ids = {t["id"] for t in pdpl_templates.list_templates()}
    assert ids == {"doc01_privacy_policy", "doc03_data_subject_rights", "doc04_ropa", "doc10_dpia",
                   "doc12_cookies", "doc13_processing_agreement", "doc14_processor_dpa"}
    assert not (ids & {"doc08_governance", "doc11_security_of_processing",
                       "reg16_dsar_log", "form08_dpo_appointment"})


def test_every_declared_placeholder_is_scanned_from_the_text_so_none_are_missed():
    """The source header under-declares slots; we scan the text. Filling the scanned set must leave
    no {{HOLE}} in any language of any template."""
    import re as _re
    for t in pdpl_templates.list_templates():
        r = render(t["id"], {p: "X" for p in t["placeholders"]}, strict=True)
        text = "\n".join(s["body"] + s["heading"] for loc in r["content"]
                         for s in r["content"][loc]["sections"])
        assert not _re.search(r"\{\{[A-Z0-9_]+\}\}", text), t["id"]


def test_render_is_bilingual_and_substitutes_every_placeholder():
    r = render("doc01_privacy_policy", _all("doc01_privacy_policy"))
    assert set(r["content"]) == {"en", "ar"}
    en = "\n".join(s["body"] for s in r["content"]["en"]["sections"])
    ar = "\n".join(s["body"] for s in r["content"]["ar"]["sections"])
    assert "{{" not in en and "{{" not in ar            # no unfilled holes
    assert "«COMPANY_NAME»" in en and "«COMPANY_NAME»" in ar   # the value landed in both languages
    assert r["content"]["en"]["title"] == "Privacy Policy"
    assert r["content"]["ar"]["title"] == "سياسة الخصوصية"    # genuinely translated


def test_a_missing_placeholder_is_refused_not_guessed():
    vals = _all("doc12_cookies")
    vals.pop(next(iter(vals)))                           # drop one required value
    with pytest.raises(TemplateError):
        render("doc12_cookies", vals, strict=True)
    # non-strict reports it instead of raising
    out = render("doc12_cookies", vals, strict=False)
    assert out["missing"]


def test_an_unknown_template_is_refused():
    with pytest.raises(TemplateError):
        render("doc99_not_real", {})


def test_render_then_publish_produces_a_filled_legal_page():
    ds = MemoryDataStore()
    legal = LegalPageService(store=LegalPageStore(ds), clock=ManualClock(1_000.0))
    r = render("doc13_processing_agreement", _all("doc13_processing_agreement"))
    run(legal.draft(tenant="t", doc_id=r["slug"], version="1.0", content=r["content"],
                    requires_acceptance=True, default_locale="ar"))
    run(legal.publish(tenant="t", doc_id=r["slug"], version="1.0"))
    view = run(legal.current_view(tenant="t", doc_id=r["slug"], locale="ar"))
    assert view["version"] == "1.0" and view["requires_acceptance"] is True
    assert "{{" not in "\n".join(s["body"] for s in view["sections"])
