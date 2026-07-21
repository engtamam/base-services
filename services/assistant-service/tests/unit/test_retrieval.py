"""Indexing and ranked retrieval.

Sample entries only — fake content, real behaviour. The claims under test are the ones the whole
phase rests on: prompt size stops tracking KB size, Arabic spelling variants still match, and the
two scoping layers hold at query time as well as on the entry.
"""
from __future__ import annotations

from app.core.indexing import chunk_all, normalize, tokenize
from app.core.kb import parse_kb
from app.core.retrieval import KbIndex, budgeted

MAX_CHARS = 800


def _index(doc, **kw):
    entries = parse_kb(doc)
    return KbIndex(entries, chunk_all(entries, max_chars=MAX_CHARS), **kw)


SAMPLE = {
    "billing.vat": {"title": {"en": "VAT number", "ar": "الرقم الضريبي"},
                    "body": {"en": "Your VAT number is fifteen digits and appears on every invoice.",
                             "ar": "الرقم الضريبي مكون من خمسة عشر رقما ويظهر على كل فاتورة."}},
    "billing.refund": {"title": "Refunds",
                       "body": "A refund returns money to the original payment method."},
    "admin.purge": {"title": "Purge records", "body": "Deletes records permanently.",
                    "surfaces": ["admin"], "requires": "records.purge"},
    "cameras.add": {"title": "Add a camera", "body": "Register a camera by its stream URL.",
                    "anchor": "settings.cameras.add"},
}


# ── normalisation: the Arabic variants a reader considers identical ───────────
def test_diacritics_are_folded():
    assert normalize("كَتَبَ") == normalize("كتب")


def test_alef_variants_fold_together():
    assert normalize("أحمد") == normalize("احمد") == normalize("إحمد") == normalize("آحمد")


def test_alef_maqsura_and_ya_fold_together():
    assert normalize("على") == normalize("علي")


def test_tatweel_is_decoration_and_is_removed():
    assert normalize("كــتــب") == normalize("كتب")


def test_english_is_case_folded():
    assert tokenize("VAT Number") == tokenize("vat number")


def test_an_arabic_question_matches_differently_spelled_help():
    """The failure this prevents is invisible: the assistant says it does not know something it
    does know, purely because the word was typed with a hamza."""
    idx = _index(SAMPLE)
    hits = idx.search("الرقم الضريبى", lang="ar")     # alef maqsura instead of ya
    assert hits and hits[0].entry.id == "billing.vat"


# ── ranking ───────────────────────────────────────────────────────────────────
def test_the_relevant_entry_outranks_the_irrelevant_one():
    idx = _index(SAMPLE)
    hits = idx.search("vat number invoice")
    assert hits[0].entry.id == "billing.vat"


def test_an_unmatched_query_returns_nothing_rather_than_a_weak_guess():
    idx = _index(SAMPLE)
    assert idx.search("quantum chromodynamics") == []


def test_an_empty_query_returns_nothing():
    assert _index(SAMPLE).search("   ") == []


def test_one_chunk_per_entry_so_a_single_page_cannot_crowd_out_the_rest():
    doc = {"long.one": {"title": "Invoices",
                        "body": "Invoices explained.\n\nInvoices again.\n\nInvoices once more."},
           "other.one": {"title": "Invoices elsewhere", "body": "Invoices also here."}}
    idx = _index(doc)
    hits = idx.search("invoices")
    assert len({h.entry.id for h in hits}) == len(hits), "an entry appeared twice"


def test_a_threshold_drops_weak_matches():
    idx = _index(SAMPLE)
    assert idx.search("refund", min_score=1000.0) == []


# ── the two scoping layers, enforced at query time ────────────────────────────
def test_an_entry_requiring_a_capability_is_not_retrievable_without_it():
    idx = _index(SAMPLE)
    assert idx.search("purge records", surface="admin", caps=frozenset()) == []
    assert idx.search("purge records", surface="admin",
                      caps=frozenset({"records.purge"}))[0].entry.id == "admin.purge"


def test_claiming_the_surface_does_not_grant_the_capability():
    """Surface is caller-declared, so it is a relevance hint. Capability is the control."""
    idx = _index(SAMPLE)
    assert idx.search("purge", surface="admin", caps=frozenset()) == []


def test_surface_scopes_which_help_is_relevant():
    idx = _index(SAMPLE)
    caps = frozenset({"records.purge"})
    assert idx.search("purge", surface="web", caps=caps) == [], "admin-only help on a web surface"
    assert idx.search("purge", surface="admin", caps=caps) != []


def test_untagged_entries_serve_every_surface():
    idx = _index(SAMPLE)
    assert idx.search("refund", surface="web") and idx.search("refund", surface="admin")


# ── the anchor path: the ? icon ───────────────────────────────────────────────
def test_anchor_lookup_finds_the_entry_directly():
    idx = _index(SAMPLE)
    assert idx.by_anchor("settings.cameras.add").id == "cameras.add"


def test_anchor_lookup_still_enforces_capability():
    doc = {"x": {"title": "T", "body": "B", "anchor": "a.b", "requires": "some.cap"}}
    idx = _index(doc)
    assert idx.by_anchor("a.b", caps=frozenset()) is None, "a direct lookup is not a bypass"
    assert idx.by_anchor("a.b", caps=frozenset({"some.cap"})) is not None


def test_an_unknown_anchor_is_none_not_an_error():
    assert _index(SAMPLE).by_anchor("nope.nothing") is None


# ── the phase's central claim ─────────────────────────────────────────────────
def test_context_size_does_not_grow_with_kb_size():
    """THE POINT OF RETRIEVAL. A KB twenty times larger must not produce a larger prompt."""
    small = {f"e.{i}": {"title": f"Entry {i}", "body": f"Body about invoices number {i}."}
             for i in range(20)}
    large = {f"e.{i}": {"title": f"Entry {i}", "body": f"Body about invoices number {i}."}
             for i in range(400)}
    budget = 2000
    s = budgeted(_index(small).search("invoices", limit=8), max_chars=budget)
    l = budgeted(_index(large).search("invoices", limit=8), max_chars=budget)
    assert sum(len(h.chunk.text) for h in s) <= budget
    assert sum(len(h.chunk.text) for h in l) <= budget, "prompt grew with the KB"


def test_the_budget_cuts_whole_passages_not_partial_ones():
    # A clipped passage can drop the sentence the answer leaned on, leaving a citation that does
    # not support the claim.
    idx = _index(SAMPLE)
    hits = budgeted(idx.search("vat refund camera", limit=8), max_chars=1)
    assert len(hits) == 1, "at least one passage is always kept, and never a fragment"
    assert hits[0].chunk.text.strip() == hits[0].chunk.text


def test_a_citation_ref_distinguishes_two_passages_of_one_entry():
    doc = {"e": {"title": "T", "body": "First para.\n\nSecond para."}}
    idx = _index(doc)
    refs = {c.ref for c in idx._chunks}
    assert len(refs) == 2 and all(r.startswith("e#") for r in refs)


def test_every_chunk_carries_its_title_so_a_passage_stands_alone():
    # "it is fifteen digits" answers nothing without "VAT number".
    idx = _index(SAMPLE)
    hit = idx.search("fifteen digits", lang="en")[0]
    assert "VAT number" in hit.chunk.text


# ── language ──────────────────────────────────────────────────────────────────
def test_an_entry_written_only_in_one_language_still_answers_the_other():
    """Otherwise the assistant reports not knowing something it knows, because the help happens
    to be written in the other language."""
    doc = {"only.ar": {"title": {"ar": "الفواتير"}, "body": {"ar": "الفاتورة تصدر شهريا."}}}
    idx = _index(doc)
    assert idx.search("الفاتورة", lang="en") != []


# ── stopwords: precision on a small corpus ────────────────────────────────────
def test_function_words_do_not_drive_a_match():
    """BM25 leans on IDF to discount common terms, and IDF is unreliable on a small corpus: in a
    handful of entries a word in one of them looks genuinely rare. That is how "how do I file tax
    in Peru" retrieved a camera page with confidence."""
    idx = _index(SAMPLE)
    assert idx.search("how do I file corporate tax in Peru", min_score=0.35) == []


def test_a_query_of_only_function_words_still_matches_something():
    # Returning nothing would be indistinguishable from "we have no help at all", which is a worse
    # answer than a weak one.
    from app.core.indexing import tokenize
    assert tokenize("how do I") != []


def test_stopwords_do_not_break_a_real_question():
    idx = _index(SAMPLE)
    assert idx.search("how do I add a camera")[0].entry.id == "cameras.add"


def test_arabic_function_words_are_dropped_too():
    from app.core.indexing import tokenize
    assert "من" not in tokenize("ما هو الرقم من الضريبي")
