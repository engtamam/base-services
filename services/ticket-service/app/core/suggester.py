"""The AI advisory hook — turns an assistant-service suggestion into a LOGGED, non-binding record.

on_created asks the provider to triage a new ticket and writes the result as an `ai_suggested` event
beside the human's log. It NEVER changes status and NEVER sends anything. The one thing it may apply
is a TAG, and only at/above the operator's confidence threshold (default: never) — a reversible,
low-stakes hint, recorded as its own `tagged` event with `ai` as the actor so it is always clear the
machine, not a person, added it.

Everything here is best-effort: a provider that is down or slow leaves the ticket exactly as it would
be with no AI. The suggestion is a map; the human is the authority.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

_AI_ACTOR = "ai"


class TicketSuggester:
    def __init__(self, *, provider, events, tickets, workflow, autotag_min_confidence: float = 1.0) -> None:
        self._provider = provider
        self._events = events
        self._tickets = tickets
        self._wf = workflow
        self._autotag_min = autotag_min_confidence

    async def on_created(self, ticket, *, body: str = "") -> None:
        triage = await self._provider.triage(
            tenant=ticket.tenant, ticket_type=ticket.type, subject=ticket.subject, body=body,
            types=tuple(self._wf.keys()))
        if triage is None:
            return
        # Record the suggestion, always. It is advice, logged beside what the human then did.
        await self._events.append(
            tenant=ticket.tenant, ticket_id=ticket.id, kind="ai_suggested", actor_ref=_AI_ACTOR,
            body=triage.summary, data=triage.to_dict())
        # The ONLY auto-application, and only above the operator's threshold: a tag. Never status.
        if triage.tags and triage.confidence >= self._autotag_min:
            fresh = [t for t in triage.tags if t and t not in ticket.tags]
            if fresh:
                ticket.tags = sorted({*ticket.tags, *fresh})
                ticket.touch()
                await self._tickets.save(ticket)
                await self._events.append(
                    tenant=ticket.tenant, ticket_id=ticket.id, kind="tagged", actor_ref=_AI_ACTOR,
                    data={"tags": ticket.tags, "by": "ai"})

    async def draft_reply(self, ticket, thread: list[dict[str, Any]]):
        """An on-demand reply draft for an agent to edit and send. Returns None if no AI is wired."""
        return await self._provider.draft(tenant=ticket.tenant, subject=ticket.subject, thread=thread)
