"""Hybrid scoring — semantic added to lexical, never replacing it.

A deterministic fake embedder stands in for a real one: these tests are about the BLEND, and a real
model would make them slow, non-deterministic and dependent on a network.
"""
from __future__ import annotations

import pytest

from app.core.indexing import chunk_all
from app.core.kb import parse_kb
from app.core.retrieval import KbIndex

DOC = {
    "billing.vat": {"title": "VAT number", "body": "Your VAT number is fifteen digits."},
    "support.contact": {"title": "Contact support",
                        "body": "Reach the team through the in-app help panel."},
}


def _index(doc=DOC):
    entries = parse_kb(doc)
    return KbIndex(entries, chunk_all(entries, max_chars=800))


def _vec(*xs):
    return list(xs)


def test_vectors_must_align_with_chunks():
    """A short batch misaligns every vector with the wrong passage. That does not read as a bug —
    it reads as the assistant being mediocre, for months."""
    idx = _index()
    with pytest.raises(ValueError, match="misaligned"):
        idx.attach_vectors([_vec(1, 0)])


def test_lexical_only_when_no_vectors_are_attached():
    idx = _index()
    assert idx.search("vat number")[0].entry.id == "billing.vat"


def test_semantic_similarity_can_outrank_a_shared_word():
    """THE GAP THE EVAL RECORDED. 'phone support number' shares the word 'number' with 'VAT
    number' and means the other thing entirely. No lexical threshold separates them."""
    idx = _index()
    # Two dimensions standing for [billing-ness, support-ness].
    idx.attach_vectors([_vec(1.0, 0.0)] * len(idx._chunks))
    order = [c.entry_id for c in idx._chunks]
    idx.attach_vectors([_vec(1.0, 0.0) if e == "billing.vat" else _vec(0.0, 1.0) for e in order])

    # A support-flavoured query, lexically pulled toward "VAT number" by the word "number".
    hits = idx.search("phone support number", query_vector=_vec(0.0, 1.0), alpha=0.3)
    assert hits and hits[0].entry.id == "support.contact"


def test_exactness_still_wins_when_the_lexical_signal_is_strong():
    """Semantic is fuzzy in both directions. A product name, an error code or a field label must
    still be found by being written exactly."""
    idx = _index()
    order = [c.entry_id for c in idx._chunks]
    idx.attach_vectors([_vec(1.0, 0.0) if e == "billing.vat" else _vec(0.0, 1.0) for e in order])
    hits = idx.search("VAT number fifteen digits", query_vector=_vec(0.0, 1.0), alpha=0.8)
    assert hits[0].entry.id == "billing.vat"


def test_alpha_of_one_is_pure_lexical():
    idx = _index()
    order = [c.entry_id for c in idx._chunks]
    idx.attach_vectors([_vec(0.0, 1.0) if e == "billing.vat" else _vec(1.0, 0.0) for e in order])
    # Even with vectors pointing the other way, alpha=1.0 must ignore them entirely.
    assert idx.search("vat number", query_vector=_vec(1.0, 0.0), alpha=1.0)[0].entry.id == "billing.vat"


def test_no_query_vector_falls_back_to_lexical():
    # This is the degradation path: the embedder was unreachable, and the answer still happens.
    idx = _index()
    order = [c.entry_id for c in idx._chunks]
    idx.attach_vectors([_vec(1.0, 0.0) for _ in order])
    assert idx.search("vat number", query_vector=None, alpha=0.3)[0].entry.id == "billing.vat"


def test_the_threshold_keeps_its_meaning_when_embeddings_are_on():
    """Blended scores are rescaled onto the lexical range, so a configured min_score does not
    silently become stricter the moment semantic scoring is switched on."""
    idx = _index()
    order = [c.entry_id for c in idx._chunks]
    idx.attach_vectors([_vec(1.0, 0.0) if e == "billing.vat" else _vec(0.0, 1.0) for e in order])
    # The threshold must sit ABOVE the un-rescaled blend ceiling (blended scores are 0..1) and
    # BELOW the real lexical score, or the test passes whether or not rescaling happens.
    lex = idx.search("vat number", min_score=1.5)
    assert lex, "fixture drifted: the lexical score must exceed the threshold for this to test"
    hyb = idx.search("vat number", query_vector=_vec(1.0, 0.0), alpha=0.5, min_score=1.5)
    assert hyb, "a threshold that passed lexically started failing once embeddings were on"


def test_vectors_are_normalised_so_magnitude_does_not_decide():
    # Magnitude differs between providers and between models from one provider; ranking must not.
    idx = _index()
    order = [c.entry_id for c in idx._chunks]
    # Chosen so the two answers DISAGREE. Raw dot products against the query (0,1):
    #   billing (10,10) -> 10        support (0,1) -> 1      => billing wins on magnitude
    # Unit-normalised:
    #   billing (.707,.707) -> .707  support (0,1) -> 1.0    => support wins on direction
    # An earlier version of this test used vectors where both answers agreed, so it passed against
    # a build with normalisation removed and proved nothing.
    idx.attach_vectors([_vec(10.0, 10.0) if e == "billing.vat" else _vec(0.0, 1.0) for e in order])
    hits = idx.search("phone support number", query_vector=_vec(0.0, 1.0), alpha=0.0)
    assert hits[0].entry.id == "support.contact", "a longer vector won on length, not meaning"
