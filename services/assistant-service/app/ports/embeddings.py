"""Embeddings port — semantic scoring, entirely optional.

WHY THIS IS OPTIONAL AND LEXICAL IS THE DEFAULT. Embeddings need either a model in-process (a large
dependency, a large image, and a licence to audit) or a network call per query and per entry (an
API key, a bill, and a failure mode). Neither belongs in the path a service takes to boot with no
configuration. So BM25 answers on its own, and this makes it better where an operator wants it to
be.

WHAT IT BUYS, precisely. Lexical retrieval matches words; it cannot tell that "phone support
number" and "VAT number" share a word and nothing else, nor that "cancel my plan" and
"end a subscription" share a meaning and no words. Those are the two failures a threshold cannot
separate, because one wants a higher bar and the other a lower one. Semantic similarity separates
them, which is why it is the fix rather than a tuned constant.

HYBRID, NOT REPLACEMENT. The two are combined rather than swapped. Lexical is exact — it will
always find a product name, an error code, a field label — and embeddings are fuzzy in both
directions, so a purely semantic index answers "what is ISO 19650" with something merely
adjacent. Keeping both means an exact term still wins on exactness.

A FAILURE HERE DEGRADES TO LEXICAL. If the embedder is unreachable the service answers from BM25
alone rather than refusing: a degraded answer with citations is worth more than an outage, and the
lexical path is a complete product on its own.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embeddings(Protocol):
    driver: str
    # Vector width. Needed to detect a model change: vectors of a different width cannot be
    # compared with stored ones, and silently comparing them produces confident nonsense.
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input, in the same order.

        Batched deliberately: an entry-per-call embedder turns indexing a thousand-entry KB into a
        thousand round trips, which is the difference between a rebuild that is unnoticeable and
        one an operator learns to avoid.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
