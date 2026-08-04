"""No AI wired — the default. Suggests nothing; the ticket proceeds exactly as without it."""
from __future__ import annotations

from typing import Any

from app.ports.suggestion_provider import Draft, Triage


class NullSuggestionProvider:
    driver = "null"

    async def triage(self, *, tenant: str, ticket_type: str, subject: str, body: str,
                     types: tuple[str, ...]) -> Triage | None:
        return None

    async def draft(self, *, tenant: str, subject: str, thread: list[dict[str, Any]]) -> Draft | None:
        return None

    async def health(self) -> bool:
        return True
