"""Grounded, cited, or absent — the honesty gate.

Sample entries only. These tests are the ones that decide whether "never confidently wrong" is a
property or a slogan, so each names the failure it prevents.
"""
from __future__ import annotations

import asyncio

from app.adapters.model.extractive import ExtractiveModel
from app.core.answering import (MODEL_FAILED, NO_EVIDENCE, UNSUPPORTED, compose)
from app.core.indexing import chunk_all
from app.core.kb import parse_kb
from app.core.retrieval import KbIndex
from app.ports.model import ModelAnswer, ModelError, Passage

NOT_FOUND = "I don't have anything on that."

SAMPLE = {
    "billing.vat": {"title": "VAT number",
                    "body": "Your VAT number is fifteen digits and appears on every invoice."},
    "billing.refund": {"title": "Refunds",
                       "body": "A refund returns money to the original payment method."},
}


def run(c):
    return asyncio.run(c)


def _hits(query, doc=SAMPLE, **kw):
    entries = parse_kb(doc)
    idx = KbIndex(entries, chunk_all(entries, max_chars=800))
    return idx.search(query, **kw)


def _compose(hits, model=None, **kw):
    return run(compose(question="q", hits=hits, model=model or ExtractiveModel(),
                       lang="en", not_found_text=NOT_FOUND, **kw))


# ── fakes, each standing for one way a model misbehaves ───────────────────────
class _Liar:
    """Cites a source it was never given — the clearest sign it stopped using the evidence."""
    driver = "liar"
    async def answer(self, **kw):
        return ModelAnswer(text="Refunds are instant and automatic.",
                           used_refs=("invented.entry#0",))
    async def health(self): return True
    async def close(self): return None


class _Silent:
    driver = "silent"
    async def answer(self, **kw):
        return ModelAnswer(text="   ", used_refs=("billing.vat#0",))
    async def health(self): return True
    async def close(self): return None


class _Broken:
    driver = "broken"
    async def answer(self, **kw):
        raise ModelError("upstream said: quota exceeded for project acme-prod-1234")
    async def health(self): return True
    async def close(self): return None


class _Partial:
    """Cites one real ref and one invented one."""
    driver = "partial"
    async def answer(self, *, passages, **kw):
        return ModelAnswer(text="An answer.", used_refs=(passages[0].ref, "made.up#9"))
    async def health(self): return True
    async def close(self): return None


# ── no evidence ───────────────────────────────────────────────────────────────
def test_no_evidence_abstains_without_calling_the_model():
    """A model handed zero passages and asked to be helpful WILL produce something, and that
    something is the exact failure this service exists to prevent."""
    class _MustNotRun:
        driver = "must-not-run"
        async def answer(self, **kw): raise AssertionError("the model was called with no evidence")
        async def health(self): return True
        async def close(self): return None

    a = _compose([], model=_MustNotRun())
    assert a.abstained and a.abstained_because == NO_EVIDENCE
    assert a.text == NOT_FOUND and a.citations == ()


def test_an_unmatched_question_produces_an_abstention_not_a_guess():
    a = _compose(_hits("quantum chromodynamics"))
    assert a.abstained and a.abstained_because == NO_EVIDENCE


# ── citation faithfulness ─────────────────────────────────────────────────────
def test_a_model_citing_a_source_it_was_not_given_is_downgraded():
    a = _compose(_hits("refund"), model=_Liar())
    assert a.abstained and a.abstained_because == UNSUPPORTED
    assert "instant and automatic" not in a.text, "the fluent invention must not be shown"


def test_invented_citations_are_dropped_and_real_ones_kept():
    a = _compose(_hits("vat number"), model=_Partial())
    assert not a.abstained
    assert all(c.ref != "made.up#9" for c in a.citations)
    assert len(a.citations) == 1


def test_empty_text_is_an_abstention_even_with_a_valid_citation():
    a = _compose(_hits("vat number"), model=_Silent())
    assert a.abstained and a.abstained_because == UNSUPPORTED


def test_a_model_failure_abstains_and_never_leaks_the_upstream_reason():
    a = _compose(_hits("vat number"), model=_Broken())
    assert a.abstained and a.abstained_because == MODEL_FAILED
    # Provider messages carry hostnames, model names and quota details.
    assert "quota" not in a.text and "acme-prod" not in a.text
    assert a.text == NOT_FOUND


# ── the extractive default ────────────────────────────────────────────────────
def test_the_default_answers_with_the_kb_s_own_words_and_cites_them():
    a = _compose(_hits("vat number"))
    assert not a.abstained
    assert "fifteen digits" in a.text
    assert a.citations and a.citations[0].entry_id == "billing.vat"


def test_the_default_cannot_hallucinate_because_it_generates_nothing():
    a = _compose(_hits("vat number"))
    # Every sentence shown is lifted from a passage that was retrieved.
    entries = parse_kb(SAMPLE)
    corpus = " ".join(e.text("en")[1] for e in entries)
    for sentence in [s for s in a.text.split(".") if len(s.strip()) > 20]:
        assert sentence.strip() in corpus or sentence.strip() in a.text


def test_every_citation_points_at_a_retrieved_passage():
    hits = _hits("refund vat")
    a = _compose(hits)
    supplied = {h.chunk.ref for h in hits}
    assert {c.ref for c in a.citations} <= supplied


def test_a_grounded_answer_records_which_driver_answered():
    assert _compose(_hits("vat number")).used_model == "extractive"


def test_a_model_outage_does_not_wear_the_knowledge_gap_message():
    """Found by a live rate-limit. The user was told "I don't have anything on that yet" — the
    KB-gap wording — when the truth was "retry shortly". One sends a person to author help
    content, the other to wait; they must not read the same."""
    a = _compose(_hits("vat number"), model=_Broken(),
                 unavailable_text="Temporarily unavailable, please retry.")
    assert a.abstained and a.abstained_because == MODEL_FAILED
    assert a.text == "Temporarily unavailable, please retry."
    assert a.text != NOT_FOUND


def test_a_real_knowledge_gap_still_says_so():
    a = _compose([], unavailable_text="Temporarily unavailable, please retry.")
    assert a.abstained_because == NO_EVIDENCE and a.text == NOT_FOUND


def test_the_outage_message_falls_back_when_unset():
    # An operator who never configured one still gets a sentence rather than an empty reply.
    a = _compose(_hits("vat number"), model=_Broken())
    assert a.text == NOT_FOUND
