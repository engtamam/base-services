"""The eval harness — and it must be able to go red.

A golden set that always passes measures nothing. Scoring answers by keyword presence over a
handful of cases reports 100% and detects no regression: the number is reassuring and the check is
decoration. By this repository's own rule, a check that cannot fail is not a check.

So this harness has a self-test. `test_a_broken_retriever_makes_the_eval_go_red` deliberately
breaks retrieval and asserts the scorecard drops. If someone later weakens the metrics — loosens a
threshold, marks a hard case optional — that self-test is what notices, because a suite that cannot
distinguish a working assistant from a broken one is worse than none: it certifies the broken one.

WHAT IS MEASURED, and why each one is separate:

  retrieval_recall     did the right evidence come back at all? A wrong answer with the right
                       passage retrieved is a synthesis problem; a wrong answer with the wrong
                       passage is a ranking problem. One number cannot tell an operator which.
  answer_correctness   does the answer contain what the gold answer requires?
  citation_validity    does every citation point at a passage that was actually retrieved?
  abstention_correct   did it abstain on the questions it SHOULD abstain on? This is the metric
                       that stops "always answer something" from scoring well.
  hallucination_rate   did it answer confidently where no evidence existed? Target zero.

The golden set is small and deliberately fake, because it tests the PIPELINE rather than any
product's help content. Real content is authored separately and gets its own set.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.adapters.model.extractive import ExtractiveModel
from app.core.answering import compose
from app.core.indexing import chunk_all
from app.core.kb import parse_kb
from app.core.retrieval import KbIndex, budgeted

NOT_FOUND = "I don't have anything on that."

GOLDEN_KB = {
    "billing.vat": {"title": "VAT number",
                    "body": "Your VAT number is fifteen digits and appears on every invoice."},
    "billing.refund": {"title": "Refunds",
                       "body": "A refund returns money to the original payment method within ten days."},
    "cameras.add": {"title": "Add a camera",
                    "body": "Register a camera by entering its stream URL in Settings."},
    "account.password": {"title": "Change your password",
                         "body": "Open Account, choose Security, then Change password."},
}


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    expect_entry: str = ""        # "" means: it SHOULD abstain
    must_contain: str = ""
    # A case we currently FAIL, recorded rather than removed. Deleting an inconvenient case is how
    # a golden set drifts into only testing what already works; the scorecard reports these so the
    # number stays honest and the gap stays visible.
    known_gap: str = ""


GOLDEN: tuple[Case, ...] = (
    Case("vat", "what is my vat number", "billing.vat", "fifteen digits"),
    Case("refund-time", "how long does a refund take", "billing.refund", "ten days"),
    Case("add-camera", "how do I add a camera", "cameras.add", "stream URL"),
    Case("password", "change my password", "account.password", "Security"),
    # Must abstain: nothing in the KB covers these. This is the half that stops "always answer
    # something" from scoring well.
    Case("unrelated-tax", "how do I file corporate tax in Peru"),
    Case("unrelated-physics", "explain quantum chromodynamics"),
    Case("plausible-but-absent", "what is your phone support number",
         known_gap="lexical retrieval matches 'number' in 'VAT number'. No global threshold "
                   "separates it: raising the bar past this score also drops the legitimate "
                   "refund case. Semantic scoring is the fix (the embeddings adapter), not a "
                   "tuned constant."),
)


def _index(kb):
    entries = parse_kb(kb)
    return KbIndex(entries, chunk_all(entries, max_chars=800))


def run(c):
    return asyncio.run(c)


def score(index, cases=GOLDEN, *, min_score: float = 0.35) -> dict:
    """Run the golden set and return the scorecard."""
    m = {"retrieval_recall": [0, 0], "answer_correctness": [0, 0],
         "citation_validity": [0, 0], "abstention_correct": [0, 0], "hallucinated": [0, 0],
         "known_gaps": [0, 0]}

    for case in cases:
        hits = budgeted(index.search(case.question, limit=6, min_score=min_score), max_chars=6000)
        answer = run(compose(question=case.question, hits=hits, model=ExtractiveModel(),
                             lang="en", not_found_text=NOT_FOUND))

        if case.expect_entry:
            m["retrieval_recall"][1] += 1
            if any(h.entry.id == case.expect_entry for h in hits):
                m["retrieval_recall"][0] += 1

            m["answer_correctness"][1] += 1
            if answer.grounded and case.must_contain.lower() in answer.text.lower():
                m["answer_correctness"][0] += 1

            m["citation_validity"][1] += 1
            supplied = {h.chunk.ref for h in hits}
            if answer.citations and all(c.ref in supplied for c in answer.citations):
                m["citation_validity"][0] += 1
        else:
            m["abstention_correct"][1] += 1
            if answer.abstained:
                m["abstention_correct"][0] += 1
            elif case.known_gap:
                m["known_gaps"][0] += 1
            m["hallucinated"][1] += 1
            if answer.grounded and not case.known_gap:
                m["hallucinated"][0] += 1     # answered where nothing supported it

    return {k: {"passed": v[0], "total": v[1],
                "pct": round(100.0 * v[0] / v[1], 1) if v[1] else 100.0}
            for k, v in m.items()}


# ── the harness on a healthy pipeline ─────────────────────────────────────────
def test_retrieval_finds_the_right_evidence():
    assert score(_index(GOLDEN_KB))["retrieval_recall"]["pct"] == 100.0


def test_answers_contain_what_the_gold_requires():
    assert score(_index(GOLDEN_KB))["answer_correctness"]["pct"] == 100.0


def test_every_citation_points_at_retrieved_evidence():
    assert score(_index(GOLDEN_KB))["citation_validity"]["pct"] == 100.0


def test_it_abstains_on_what_it_should_not_know():
    """Every should-abstain case abstains, EXCEPT the ones recorded as known gaps.

    The baseline is asserted exactly rather than loosely: if a future change makes it abstain on
    the known gap too, this goes red and the gap should be closed in the golden set. A `>=` here
    would let the number quietly improve or decay without anyone noticing either.
    """
    card = score(_index(GOLDEN_KB))
    expected_gaps = sum(1 for c in GOLDEN if c.known_gap)
    assert card["abstention_correct"]["passed"] == card["abstention_correct"]["total"] - expected_gaps
    assert card["known_gaps"]["passed"] == expected_gaps


def test_the_hallucination_rate_is_zero_outside_the_recorded_gaps():
    """Zero unexplained confident answers. A known gap is excluded from this number and counted in
    its own — folding it in here would let a real hallucination hide behind a documented one."""
    assert score(_index(GOLDEN_KB))["hallucinated"]["passed"] == 0


def test_every_known_gap_states_why_it_is_still_open():
    # A gap without a reason is a silently-lowered bar.
    for case in GOLDEN:
        if case.known_gap:
            assert len(case.known_gap) > 40, f"{case.id}: record WHY it is open"


# ── the self-test: this suite must be ABLE to fail ────────────────────────────
def test_a_broken_retriever_makes_the_eval_go_red():
    """THE POINT. A golden set that cannot go red certifies a broken assistant.

    Retrieval is disabled by demanding an impossible relevance score. Recall must collapse — if it
    does not, the metric is not measuring retrieval and every green run above is meaningless.
    """
    broken = score(_index(GOLDEN_KB), min_score=10_000.0)
    assert broken["retrieval_recall"]["pct"] == 0.0
    assert broken["answer_correctness"]["pct"] == 0.0


def test_an_assistant_that_always_answers_scores_badly_on_abstention():
    """The failure mode this metric exists for: a chatty assistant looks good on every metric that
    only rewards answering, so abstention is scored separately and explicitly."""
    class _AlwaysAnswers:
        driver = "always"
        async def answer(self, *, passages, **kw):
            from app.ports.model import ModelAnswer
            return ModelAnswer(text="Certainly! Here is an answer.",
                               used_refs=tuple(p.ref for p in passages))
        async def health(self): return True
        async def close(self): return None

    index = _index(GOLDEN_KB)
    # A threshold of zero lets weak matches through, which is how an eager pipeline answers
    # questions it has no evidence for.
    abstained = 0
    for case in (c for c in GOLDEN if not c.expect_entry):
        hits = budgeted(index.search(case.question, limit=6, min_score=0.0), max_chars=6000)
        a = run(compose(question=case.question, hits=hits, model=_AlwaysAnswers(),
                        lang="en", not_found_text=NOT_FOUND))
        abstained += 1 if a.abstained else 0
    assert abstained < 3, "with no threshold and an eager model, abstention should degrade"


def test_the_threshold_is_what_buys_abstention():
    """Ties the precision lever to the metric it moves, so nobody 'tunes' it to zero for recall
    without seeing what it costs."""
    strict = score(_index(GOLDEN_KB), min_score=0.35)
    loose = score(_index(GOLDEN_KB), min_score=0.0)
    assert strict["abstention_correct"]["pct"] >= loose["abstention_correct"]["pct"]
