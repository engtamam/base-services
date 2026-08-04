"""SuggestionProvider port — the ADVISORY AI seam. assistant-service, kept out of the authority path.

Everything here is a suggestion a human confirms. The provider can propose a triage (type / priority
/ tags) for a new ticket and draft a reply for an agent to edit — it NEVER transitions, closes, or
sends anything. A provider that is unreachable or slow returns nothing and the ticket proceeds
exactly as if no AI were wired: advisory means the ticket never depends on it.

The default driver is `null` (no AI). Content handed to a model is tenant-scoped; the pseudonym, not
the person, identifies the raiser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Triage:
    """A suggested classification. `confidence` is 0..1; nothing is applied on the strength of it
    except a tag, and only above the operator's threshold."""
    type: str = ""
    priority: str = ""
    tags: tuple[str, ...] = ()
    confidence: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "priority": self.priority, "tags": list(self.tags),
                "confidence": self.confidence, "summary": self.summary}


@dataclass(frozen=True)
class Draft:
    text: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": self.confidence}


@runtime_checkable
class SuggestionProvider(Protocol):
    driver: str

    async def triage(self, *, tenant: str, ticket_type: str, subject: str, body: str,
                     types: tuple[str, ...]) -> Triage | None:
        """Propose a classification for a new ticket, or None if the model has nothing / is down."""
        ...

    async def draft(self, *, tenant: str, subject: str, thread: list[dict[str, Any]]) -> Draft | None:
        """Draft a reply from the thread, for an agent to edit and send. None if unavailable."""
        ...

    async def health(self) -> bool: ...
