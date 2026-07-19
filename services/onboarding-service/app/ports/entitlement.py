"""Entitlement port — what completing a requirement actually GIVES someone.

"GCP style" is one option, not the model. The platform-shaped question is: a requirement is met,
so what does the person get? The answer differs by platform and must not be baked in:

    wallet_credit   money-like credit in a wallet — the GCP $300 pattern. Needs wallet-service.
    trial_period    time-limited full access. Needs nothing but a clock.
    plan            put them on a plan/tier. Needs billing.
    quota           N calls, N GB. Needs feature-flag or quota.
    null            grants nothing, and SAYS so.

A platform with no wallet declares `trial_period` and the same flow works unchanged. That is the
plug-and-play property: the flow declares WHAT is granted, config decides HOW.

**The failure this port is shaped against: telling a customer they have something they do not.**
A grant that could not be delivered must never report success. "You have 2000 SAR of credit" when
the wallet call failed is worse than an error — the customer plans around it, spends against it,
and discovers the truth at the till. So `issue` returns a receipt whose `granted` is only true when
the backend confirmed, and `null` returns granted=False rather than pretending.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

GRANTED = "granted"
ALREADY = "already_granted"       # idempotent replay — not an error, and not a second grant
REFUSED = "refused"               # a rule said no (once_per already used, unverified identity)
FAILED = "failed"                 # we could not deliver it


@dataclass(frozen=True)
class GrantRequest:
    kind: str                     # the flow's `grants` value, e.g. "welcome"
    tenant: str
    subject: str
    # The semantic idempotency key. Same key twice = same grant, never two. Built from once_per, so
    # it survives whatever the retry was.
    idempotency_key: str
    amount: int = 0               # minor units, for money-like grants
    currency: str = ""
    days: int = 0                 # for time-limited grants
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    status: str
    reference: str = ""           # the backend's own id, so an auditor can go and look
    detail: str = ""

    @property
    def granted(self) -> bool:
        """True only when the backend CONFIRMED. `already` is deliberately included: from the
        caller's side the person does have it, and treating a replay as failure would make a
        retried request look like a lost grant."""
        return self.status in (GRANTED, ALREADY)


@runtime_checkable
class Entitlement(Protocol):
    driver: str

    async def issue(self, request: GrantRequest) -> Receipt:
        """Deliver the grant. MUST NOT raise, and must be idempotent on idempotency_key."""
        ...
