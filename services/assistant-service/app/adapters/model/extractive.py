"""Extractive model — answers with the KB's own words, and generates nothing.

This is the DEFAULT, and it is a real answer rather than a placeholder. It returns the passages
retrieval ranked highest, verbatim, each with its citation. For the questions a help assistant
actually gets — "what is this field", "how do I add a camera", "what does this status mean" — the
KB sentence IS the answer, and quoting it is better than paraphrasing it.

Three properties fall out of generating nothing, and each is worth more than fluency:

  * It cannot hallucinate. There is no model to invent a plan that does not exist.
  * It costs nothing and adds no latency, so an adopter can leave it on everywhere.
  * Every sentence is quoted from a citable passage, so the citation is exact by construction
    rather than by a verification step that might be wrong.

Configuring a provider buys phrasing — joining two passages into one paragraph, answering in the
user's register. It does not buy correctness, and an adopter who never configures one still has a
working, honest help system.
"""
from __future__ import annotations

from app.ports.model import ModelAnswer, Passage


class ExtractiveModel:
    driver = "extractive"

    def __init__(self, *, max_passages: int = 3) -> None:
        # More than a few quoted passages stops being an answer and becomes a search result page.
        self._max = max(1, max_passages)

    async def answer(self, *, question: str, passages: tuple[Passage, ...], lang: str,
                     persona: str = "", max_tokens: int = 0) -> ModelAnswer:
        kept = passages[: self._max]
        # The passage text already carries its title (indexing prepends it), so quoting the body
        # alone would drop the context that makes a lifted passage make sense.
        body = "\n\n".join(p.text.strip() for p in kept)
        return ModelAnswer(text=body, used_refs=tuple(p.ref for p in kept))

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
