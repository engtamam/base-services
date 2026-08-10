"""Legal/content pages — versioned, immutable-once-published, structured for a viewer, bilingual."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.legal_pages import LegalError, LegalPageService
from app.core.stores import LegalPageStore


def run(c):
    return asyncio.run(c)


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event, payload):
        self.events.append((event, payload))


def _svc(clock=None, bus=None):
    return LegalPageService(store=LegalPageStore(MemoryDataStore()),
                            clock=clock or ManualClock(1_000.0), event_bus=bus)


# Rich bilingual content: ONE set of blocks (fields carry {en,ar}) + a title{en,ar}. The renderer
# picks the language at read time, so there is no per-locale copy to drift.
_AR_EN = {
    "title": {"en": "Privacy Policy", "ar": "سياسة الخصوصية"},
    "blocks": [{"section": {"text": {"en": "We keep little.", "ar": "نحتفظ بالقليل."}}}],
    "values": {},
}


def test_draft_then_publish_becomes_current_and_viewable_in_both_languages():
    bus = _Bus()
    svc = _svc(bus=bus)
    run(svc.draft(tenant="t", doc_id="privacy-policy", version="1.0", content=_AR_EN,
                  requires_acceptance=True, default_locale="en"))
    assert run(svc.current_view(tenant="t", doc_id="privacy-policy")) is None   # not until published
    run(svc.publish(tenant="t", doc_id="privacy-policy", version="1.0"))

    en = run(svc.current_view(tenant="t", doc_id="privacy-policy", locale="en"))
    ar = run(svc.current_view(tenant="t", doc_id="privacy-policy", locale="ar"))
    assert en["title"] == "Privacy Policy" and en["version"] == "1.0"
    assert ar["title"] == "سياسة الخصوصية" and ar["blocks"]  # blocks carried, language picked at render
    assert en["requires_acceptance"] is True
    assert ("legal.published", {"doc_id": "privacy-policy", "version": "1.0",
                                "requires_acceptance": True}) in bus.events


def test_a_published_version_is_immutable():
    svc = _svc()
    run(svc.draft(tenant="t", doc_id="terms", version="1.0", content=_AR_EN))
    run(svc.publish(tenant="t", doc_id="terms", version="1.0"))
    with pytest.raises(LegalError):
        run(svc.draft(tenant="t", doc_id="terms", version="1.0", content=_AR_EN))   # frozen


def test_publishing_a_new_version_moves_current_but_keeps_the_old_readable():
    svc = _svc()
    run(svc.draft(tenant="t", doc_id="terms", version="1.0", content=_AR_EN))
    run(svc.publish(tenant="t", doc_id="terms", version="1.0"))
    v2 = {"title": {"en": "Terms v2"}, "blocks": [], "values": {}}
    run(svc.draft(tenant="t", doc_id="terms", version="2.0", content=v2, requires_acceptance=True))
    run(svc.publish(tenant="t", doc_id="terms", version="2.0"))

    assert run(svc.current_view(tenant="t", doc_id="terms"))["version"] == "2.0"
    # the exact text someone accepted at v1 is still retrievable — the proof stays truthful
    old = run(svc.get_view(tenant="t", doc_id="terms", version="1.0", locale="en"))
    assert old["title"] == "Privacy Policy"


def test_current_is_written_not_sorted_so_1_10_beats_1_9():
    svc = _svc()
    for v in ("1.9", "1.10"):
        run(svc.draft(tenant="t", doc_id="policy", version=v, content={"title": {"en": v}, "blocks": [], "values": {}}))
        run(svc.publish(tenant="t", doc_id="policy", version=v))
    # 1.10 was published last; a text sort would wrongly pick 1.9
    assert run(svc.current_view(tenant="t", doc_id="policy"))["version"] == "1.10"


def test_publishing_a_missing_version_is_refused():
    svc = _svc()
    with pytest.raises(LegalError):
        run(svc.publish(tenant="t", doc_id="ghost", version="9.9"))


def test_view_falls_back_when_a_locale_is_missing():
    svc = _svc()
    run(svc.draft(tenant="t", doc_id="cookie", version="1.0",
                  content={"title": {"en": "Cookies"}, "blocks": [], "values": {}}, default_locale="en"))
    run(svc.publish(tenant="t", doc_id="cookie", version="1.0"))
    # asked for Arabic, which the TITLE doesn't have — the title falls back to English, never blank,
    # while the reader's chosen locale is preserved (the block renderer resolves each field per-locale).
    v = run(svc.current_view(tenant="t", doc_id="cookie", locale="ar"))
    assert v["title"] == "Cookies" and v["locale"] == "ar"


def test_list_and_history_reflect_current_and_all_versions():
    svc = _svc()
    run(svc.draft(tenant="t", doc_id="terms", version="1.0", content=_AR_EN))
    run(svc.publish(tenant="t", doc_id="terms", version="1.0"))
    run(svc.draft(tenant="t", doc_id="terms", version="2.0", content=_AR_EN, requires_acceptance=True))
    run(svc.publish(tenant="t", doc_id="terms", version="2.0"))

    docs = run(svc.list_documents(tenant="t"))
    assert len(docs) == 1 and docs[0]["version"] == "2.0" and docs[0]["requires_acceptance"] is True
    hist = run(svc.history(tenant="t", doc_id="terms"))
    assert {h["version"] for h in hist} == {"1.0", "2.0"}


def test_tenants_are_isolated():
    svc = _svc()
    run(svc.draft(tenant="a", doc_id="terms", version="1.0", content=_AR_EN))
    run(svc.publish(tenant="a", doc_id="terms", version="1.0"))
    assert run(svc.current_view(tenant="b", doc_id="terms")) is None
