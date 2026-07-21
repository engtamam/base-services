"""Model port — turning retrieved passages into prose.

THE DEFAULT DRIVER ANSWERS WITHOUT A MODEL, and that is a product decision rather than a
convenience for tests. A help assistant that requires an API key before it does anything is a
feature an adopter evaluates later and usually never switches on. The `null` driver is extractive:
it returns the passages retrieval already found, verbatim, with their citations. That is a working
help system — genuinely useful, completely free, and impossible to hallucinate with, because no
text is generated. Configuring a provider upgrades the phrasing; it is not what makes it work.

WHAT A DRIVER IS ALLOWED TO DO is deliberately narrow. It receives a question and a set of
passages, and returns prose. It does not retrieve, does not decide whether there is enough evidence
to answer, and does not choose to abstain — those are decisions the service owns, because they must
hold identically whichever provider is configured. A provider swap must never change whether the
assistant is willing to guess.

FAILURES ARE THE SERVICE'S TO PHRASE. An adapter raises `ModelError`; it never returns an upstream
error string for the caller to see. Provider messages carry internal hostnames, model names, quota
details and occasionally fragments of the prompt, and none of that belongs in an HTTP body reaching
a browser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class ModelError(RuntimeError):
    """The model could not answer. Carries a safe, internal-only reason; the API layer decides
    what the caller is told, and it is never this string."""


@dataclass(frozen=True)
class Passage:
    """One piece of evidence handed to the model, with the citation the answer must carry."""

    ref: str          # e.g. "billing.vat#0" — stable, and points at a passage a reader can open
    title: str
    text: str


@dataclass(frozen=True)
class ModelAnswer:
    text: str
    # Refs the answer actually used. The service checks these against what it supplied: a model
    # citing something it was not given is the clearest signal that it stopped using the evidence.
    used_refs: tuple[str, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class Model(Protocol):
    driver: str

    async def answer(self, *, question: str, passages: tuple[Passage, ...], lang: str,
                     persona: str = "", max_tokens: int = 0) -> ModelAnswer:
        """Compose an answer from `passages` and nothing else.

        `passages` is never empty — the service abstains before calling, so a driver never has to
        decide what to do with no evidence, and can never be the component that invents an answer
        to fill the gap.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
