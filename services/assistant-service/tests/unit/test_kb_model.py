"""The KB entry contract.

Sample entries only — deliberately tiny and obviously fake. Real help content is authored later,
one entry at a time; a test fixture that grows into the product's actual KB is a fixture nobody
dares change.

What these tests pin down is the part that is expensive to get wrong later: what is required, what
an omitted field means, and which of the two scoping layers is a control rather than a hint.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.knowledge_source.memory import MemoryKnowledgeSource
from app.core.kb import ALL_SURFACES, KbError, parse_entry, parse_kb

SAMPLE = {
    "sample.thing": {"title": {"en": "A thing", "ar": "شيء"},
                     "body": {"en": "What the thing is.", "ar": "ما هو الشيء."}},
    "sample.admin_only": {"title": "Admin thing", "body": "Only admins see this.",
                          "surfaces": ["admin"], "requires": "sample.manage"},
    "sample.field": {"title": "A field", "body": "Help for one input.",
                     "anchor": "settings.sample.field", "kind": "field"},
}


def run(c):
    return asyncio.run(c)


# ── what an author must write, and what they may omit ─────────────────────────
def test_title_and_body_are_the_only_required_fields():
    e = parse_entry("x.y", {"title": "T", "body": "B"})
    assert e.text("en") == ("T", "B")
    # everything else has a working default — a first KB is ids, titles and text
    assert e.surfaces == (ALL_SURFACES,) and not e.requires and not e.anchor and not e.kind


def test_a_bare_string_is_accepted_as_the_default_language():
    # Refusing this would make the smallest possible entry a schema error.
    assert parse_entry("x", {"title": "T", "body": "B"}).title == {"en": "T"}


def test_detail_is_accepted_as_an_alias_for_body():
    # "body" and "detail" are equally natural names; an author should not lose time to that.
    assert parse_entry("x", {"title": "T", "detail": "D"}).text("en")[1] == "D"


@pytest.mark.parametrize("bad,why", [
    ({"body": "B"}, "missing title"),
    ({"title": "T"}, "missing body"),
    ({"title": "T", "body": ""}, "empty body"),
    ({"title": "T", "body": {"en": ""}}, "empty translation"),
    ({"title": "T", "body": "B", "surfaces": []}, "empty surfaces list"),
])
def test_an_entry_that_cannot_answer_is_refused_at_parse_time(bad, why):
    with pytest.raises(KbError):
        parse_entry("x", bad)


def test_an_empty_translation_is_refused_rather_than_stored():
    # Missing falls back to another language; empty would answer with nothing while looking present.
    with pytest.raises(KbError, match="omit the language"):
        parse_entry("x", {"title": "T", "body": {"en": "B", "ar": "   "}})


# ── language ──────────────────────────────────────────────────────────────────
def test_language_falls_back_rather_than_returning_nothing():
    e = parse_entry("x", {"title": {"en": "T"}, "body": {"en": "B"}})
    assert e.text("ar") == ("T", "B"), "an untranslated entry must not become an invisible hole"


def test_a_regional_tag_finds_its_base_language():
    e = parse_entry("x", {"title": {"ar": "ع"}, "body": {"ar": "ن"}})
    assert e.text("ar-SA") == ("ع", "ن")


def test_an_unknown_language_prefers_the_DEFAULT_over_an_arbitrary_one():
    """The default-language step is load-bearing only when the default is not first in the map.
    Without it the reader gets whichever language happened to be authored first, which for a
    multilingual KB is effectively random."""
    e = parse_entry("x", {"title": {"ar": "ع", "en": "T"}, "body": {"ar": "ن", "en": "B"}})
    assert e.text("de") == ("T", "B")


def test_the_asked_language_wins_when_present():
    e = parse_entry("x", {"title": {"en": "T", "ar": "ع"}, "body": {"en": "B", "ar": "ن"}})
    assert e.text("ar") == ("ع", "ن")


# ── surface: relevance ────────────────────────────────────────────────────────
def test_an_untagged_entry_serves_every_surface():
    # This is what lets the glossary be authored ONCE instead of copied per UI.
    e = parse_entry("x", {"title": "T", "body": "B"})
    assert e.serves("admin") and e.serves("web") and e.serves("anything")


def test_a_tagged_entry_serves_only_its_surfaces():
    e = parse_entry("x", {"title": "T", "body": "B", "surfaces": ["admin", "ops"]})
    assert e.serves("admin") and e.serves("ops") and not e.serves("web")


# ── capability: authorisation ─────────────────────────────────────────────────
def test_an_entry_requiring_a_capability_is_invisible_without_it():
    e = parse_entry("x", {"title": "T", "body": "B", "requires": "billing.manage"})
    assert e.visible_to(frozenset({"billing.manage"}))
    assert not e.visible_to(frozenset()), "help must not describe what the caller cannot do"


def test_capability_is_the_control_and_surface_is_only_a_hint():
    """THE LAYERING. A client declares its own surface, so surface can be claimed. Capability
    cannot. An admin entry must stay invisible to a caller who merely SAYS it is the admin app."""
    e = parse_entry("x", {"title": "T", "body": "B",
                          "surfaces": ["admin"], "requires": "sample.manage"})
    assert e.serves("admin"), "claiming the surface passes the relevance filter"
    assert not e.visible_to(frozenset()), "but the capability filter still refuses it"


def test_an_entry_without_requires_is_visible_to_anyone():
    assert parse_entry("x", {"title": "T", "body": "B"}).visible_to(frozenset())


# ── documents ─────────────────────────────────────────────────────────────────
def test_both_the_map_and_the_list_document_shapes_load():
    as_map = parse_kb(SAMPLE)
    as_list = parse_kb([{"id": k, **v} for k, v in SAMPLE.items()])
    assert {e.id for e in as_map} == {e.id for e in as_list} == set(SAMPLE)


def test_a_duplicate_id_is_refused_because_one_would_silently_win():
    with pytest.raises(KbError, match="duplicate"):
        parse_kb([{"id": "same", "title": "A", "body": "B"},
                  {"id": "same", "title": "C", "body": "D"}])


def test_one_bad_entry_fails_the_whole_load():
    # Skipping it would leave an operator with a KB quietly missing the answer they wrote,
    # discoverable only when a user asks and is told nobody knows.
    with pytest.raises(KbError):
        parse_kb({"ok": {"title": "T", "body": "B"}, "bad": {"title": "T"}})


# ── the snapshot generation ───────────────────────────────────────────────────
def test_identical_content_yields_the_same_generation():
    a = run(MemoryKnowledgeSource(raw=SAMPLE).load())
    b = run(MemoryKnowledgeSource(raw=SAMPLE).load())
    assert a.generation == b.generation, "identical content must not churn the index"


def test_changed_content_changes_the_generation():
    other = {**SAMPLE, "sample.thing": {"title": "A thing", "body": "CHANGED"}}
    a = run(MemoryKnowledgeSource(raw=SAMPLE).load())
    b = run(MemoryKnowledgeSource(raw=other).load())
    assert a.generation != b.generation


def test_entry_order_does_not_change_the_generation():
    reversed_doc = dict(reversed(list(SAMPLE.items())))
    a = run(MemoryKnowledgeSource(raw=SAMPLE).load())
    b = run(MemoryKnowledgeSource(raw=reversed_doc).load())
    assert a.generation == b.generation, "reordering is not a content change"
