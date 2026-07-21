"""KnowledgeSource port — where the assistant's facts come from.

THE FACTS ARE NEVER SUPPLIED BY THE CALLER. That is the whole reason this port exists rather than a
`kb` field on the ask request. An assistant speaks with the product's voice; whoever controls its
knowledge controls what the product appears to assert. If the client posts the KB, then any client
— a browser console, a compromised page, a curious user — can make the product state that refunds
are automatic, that a plan includes something it does not, or that an admin action is safe. The
answer would be perfectly "grounded" in facts the attacker wrote.

So the KB is loaded from a source an OPERATOR configured. A caller may say which surface it is, what
language it wants, and what it is asking. It may not say what is true.

GENERATION, NOT TIMESTAMPS. `load()` returns a snapshot with a `generation` string. The index is
rebuilt when the generation changes and reused when it does not. A content hash — rather than an
mtime — means a file rewritten with identical bytes does not churn the index, and a file whose
clock went backwards still does.

FAIL LOUD AT LOAD, NOT AT ASK. An unreachable or malformed source raises here. The alternative —
serving an empty KB — turns a deployment mistake into an assistant that politely tells every user
it does not know anything, which reads like a working feature and can survive for weeks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.kb import KbEntry


@dataclass(frozen=True)
class KbSnapshot:
    """A whole KB at one moment, plus the key that says whether it changed."""

    entries: tuple[KbEntry, ...]
    # Changes exactly when the content changes. The indexer compares this and nothing else.
    generation: str
    # Which adapter produced it, for the manifest and for operator diagnosis.
    driver: str = ""

    def __len__(self) -> int:
        return len(self.entries)


@runtime_checkable
class KnowledgeSource(Protocol):
    driver: str

    async def load(self) -> KbSnapshot:
        """Every entry this source holds.

        Raises rather than returning an empty snapshot when the source is unreachable or invalid:
        "the KB is empty" and "the KB could not be read" produce the same silence to a user and
        must not produce the same signal to an operator.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
