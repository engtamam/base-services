"""Null — no hold protection, and it says so rather than pretending.

Returns None, which the caller treats as "do not purge this partition". That is the fail-closed
reading and it is the right default for a control over deletion: a null driver that returned an
EMPTY hold set would assert "nobody is held", and the purge would proceed to delete everything
expired — including records under legal hold — while every log line said success.

An empty set and an unknown set look identical in the data and are opposites in meaning. This one
is unknown.
"""
from __future__ import annotations

from typing import Any


class NullHoldRefs:
    driver = "null"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def for_tenant(self, tenant: str) -> dict[str, Any] | None:
        self.asked.append(tenant)
        return None
