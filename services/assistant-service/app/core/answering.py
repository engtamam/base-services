"""Composing an answer — grounded, cited, or absent.

THE SERVICE DECIDES WHETHER TO ANSWER. Not the model. Retrieval runs, the evidence is weighed, and
if it is too thin the assistant says so and names what is missing — before any driver is called.
Putting that decision in the service rather than in a prompt is what makes it hold identically for
every provider, and what makes "it must not guess" a property of the system instead of an
instruction a model may ignore under pressure.

ABSTENTION IS A CORRECT OUTCOME. A help assistant that always produces something trains users to
distrust all of it, because the confident wrong answers are indistinguishable from the right ones
until someone acts on one. Saying "I don't have anything on that, here is what I searched" costs a
user ten seconds; a plausible invention about a refund policy costs a support ticket and sometimes
money.

CITATIONS ARE CHECKED, NOT REQUESTED. A model is asked to cite, and then the refs it returns are
intersected with the refs it was given. Anything else is dropped, and an answer left with no
surviving citation is downgraded to an abstention. A model that cites a source it was never handed
has stopped using the evidence, and that is precisely the moment to stop trusting the sentence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.retrieval import Hit
from app.ports.model import Model, ModelError, Passage

# Why an answer was not given. Distinct values because they need distinct fixes: an operator
# reading "no_evidence" adds KB content, one reading "unsupported" looks at the model.
NO_EVIDENCE = "no_evidence"
UNSUPPORTED = "unsupported"
MODEL_FAILED = "model_failed"


@dataclass(frozen=True)
class Citation:
    ref: str
    entry_id: str
    title: str


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[Citation, ...] = ()
    grounded: bool = True
    # Set only when `grounded` is False — the machine-readable reason, for metrics and for the
    # operator. The user-facing sentence is `text`.
    abstained_because: str = ""
    used_model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return not self.grounded


def _citations(hits: list[Hit], refs: set[str], lang: str) -> tuple[Citation, ...]:
    out = []
    for hit in hits:
        if hit.chunk.ref in refs:
            title, _ = hit.entry.text(lang)
            out.append(Citation(ref=hit.chunk.ref, entry_id=hit.entry.id, title=title))
    return tuple(out)


async def compose(
    *,
    question: str,
    hits: list[Hit],
    model: Model,
    lang: str,
    not_found_text: str,
    unavailable_text: str = "",
    persona: str = "",
    max_tokens: int = 0,
) -> Answer:
    """Turn ranked evidence into an answer, or decline.

    `hits` has already been filtered to what this caller may see and trimmed to the context budget.
    This function's only job is the honesty gate around the model call.
    """
    if not hits:
        # No driver is called. A model handed zero passages and asked to be helpful will produce
        # something, and that something is the exact failure mode this service exists to avoid.
        return Answer(text=not_found_text, grounded=False, abstained_because=NO_EVIDENCE)

    passages = tuple(
        Passage(ref=h.chunk.ref, title=h.entry.text(lang)[0], text=h.chunk.text) for h in hits
    )

    try:
        result = await model.answer(question=question, passages=passages, lang=lang,
                                    persona=persona, max_tokens=max_tokens)
    except ModelError:
        # The upstream reason is deliberately not propagated to the caller: provider messages carry
        # hostnames, model names and quota details, and sometimes fragments of the prompt.
        #
        # But it gets its OWN sentence. A live rate-limit showed this: the user was told "I don't
        # have anything on that yet", which is the KB-gap message, so the honest reading is "write
        # more help content" when the truth was "retry in a minute". Two different failures must
        # not wear the same words — one sends a person to author documentation, the other to wait.
        return Answer(text=unavailable_text or not_found_text, grounded=False,
                      abstained_because=MODEL_FAILED)

    supplied = {p.ref for p in passages}
    # Intersect, never trust. A ref the model was not given is either an invention or a leak from
    # its training data; both mean it is no longer answering from the evidence.
    honest = {r for r in result.used_refs if r in supplied}

    if not result.text.strip():
        return Answer(text=not_found_text, grounded=False, abstained_because=UNSUPPORTED,
                      used_model=model.driver)

    if not honest:
        # Text with no surviving citation is exactly the "confidently wrong" shape: fluent, on
        # topic, and traceable to nothing. It is downgraded rather than shown.
        return Answer(text=not_found_text, grounded=False, abstained_because=UNSUPPORTED,
                      used_model=model.driver,
                      metadata={"claimed_refs": list(result.used_refs)})

    return Answer(
        text=result.text.strip(),
        citations=_citations(hits, honest, lang),
        grounded=True,
        used_model=model.driver,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
