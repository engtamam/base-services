"""Null source — answers UNKNOWN, never satisfied.

Used for a service this platform has not configured. It cannot answer, so it says so: a flow whose
requirements are all UNKNOWN never completes, which is visibly broken rather than silently
permissive. The alternative — treating an unconfigured source as satisfied — would let a platform
complete every onboarding and issue every grant while connected to nothing.
"""
from __future__ import annotations

from app.ports.requirement_source import UNKNOWN, Answer


class NullRequirementSource:
    driver = "null"

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    async def check(self, *, fact: str, tenant: str, subject: str) -> Answer:
        self.asked.append((fact, tenant, subject))
        return Answer(status=UNKNOWN,
                      detail="no source configured for this service; nothing was checked")
