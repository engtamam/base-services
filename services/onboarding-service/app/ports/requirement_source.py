"""RequirementSource port — ask the service that OWNS a fact whether it is true.

Onboarding never stores "email verified". identity-service owns that, and a second copy here would
drift — and the drift always favours "satisfied", because the copy is written when the fact becomes
true and nothing writes it back when the fact stops being true. Someone removes their card, lets a
registration lapse, has a verification expire: the owning service knows, and our cached tick does
not. The customer then passes a gate they no longer meet.

So a requirement is EVALUATED, never remembered. Three outcomes and no fourth:

    satisfied     the owning service says yes, right now
    unsatisfied   the owning service says no
    unknown       we could not ask

**`unknown` is never `satisfied`.** That is the entire safety property. An onboarding service that
treats an unreachable identity-service as "probably verified" hands out whatever completing the
flow grants — and does it precisely when the platform is already unhealthy, which is when nobody is
watching. It is also never `unsatisfied`, because blocking every customer during a blip is its own
outage, so the caller sees the ambiguity and decides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

SATISFIED = "satisfied"
UNSATISFIED = "unsatisfied"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Answer:
    status: str
    detail: str = ""

    @property
    def is_satisfied(self) -> bool:
        """Written as `== SATISFIED` rather than `!= UNSATISFIED` on purpose: the second form makes
        every future status default to satisfied, and `unknown` would silently mean yes."""
        return self.status == SATISFIED


@runtime_checkable
class RequirementSource(Protocol):
    driver: str

    async def check(self, *, fact: str, tenant: str, subject: str) -> Answer:
        """Is `fact` true for this subject right now? MUST NOT raise — an owning service being
        down must not take out the endpoint asking about it."""
        ...
