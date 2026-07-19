"""Verifier port — ask one provider whether one claim is true.

The failure this port is shaped to prevent is a **false VERIFIED**. Everything else it can get
wrong is recoverable: a wrongly refused customer complains and gets re-checked, an outage retries.
A wrongly verified identity is trust granted to whoever asked for it, and nothing downstream can
detect it — the welcome gift is issued, the business account is opened, the audit log faithfully
records that we verified them.

So there are four outcomes and no fifth, and in particular there is no "assume yes":

    verified  the provider affirmed the claim, and `evidence` says who affirmed it and when.
    refused   the provider answered, and the answer was NO.
    pending   the provider will answer later — Nafath-style flows push a prompt to a phone and
              the human decides in another app. Pending is a real state, not a soft failure.
    error     we could not ask. We know NOTHING, and must not pretend otherwise.

**refused and error must never merge.** They look similar from a caller's chair and they are
opposites. `refused` is an answer; `error` is the absence of one. Merge them and you must pick a
single behaviour for both: block on error and one provider outage locks out every legitimate
customer; allow on refused and you admit exactly the person the check exists to stop.

`verify` never raises. A provider being down must not take out the endpoint that was asking about
it — the caller gets `error` and decides what that means for their flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

VERIFIED = "verified"
REFUSED = "refused"
PENDING = "pending"
ERROR = "error"


@dataclass(frozen=True)
class Claim:
    """What is being asserted, in the form the provider needs to check it.

    `value` is the raw identifier — a national id, a CR number — and it exists ONLY in memory, for
    the length of one call. It is what goes to the provider; it is never what gets stored.
    """
    claim_type: str
    value: str
    tenant: str = ""
    subject: str = ""
    extra: dict[str, Any] = field(default_factory=dict)   # provider-specific fields, from config


@dataclass(frozen=True)
class Outcome:
    status: str                                  # VERIFIED | REFUSED | PENDING | ERROR
    provider: str = ""
    reference: str = ""                          # the provider's own id, so an auditor can go back
    detail: str = ""
    # For PENDING: whatever the adapter needs to recognise the callback later. Opaque here on
    # purpose — the port must not know what a given provider's correlation token looks like.
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        """True ONLY on an explicit affirmation. Deliberately not `status != REFUSED`: written
        that way, every new status would default to trusted, and `error` would mean yes."""
        return self.status == VERIFIED

    @property
    def is_answered(self) -> bool:
        """The provider gave us a real answer, either way. Distinguishes 'they said no' from
        'we could not ask', which is the split callers most often need."""
        return self.status in (VERIFIED, REFUSED)


@runtime_checkable
class Verifier(Protocol):
    driver: str

    async def verify(self, claim: Claim) -> Outcome:
        """Check one claim. MUST NOT raise — a provider outage must not take out the caller."""
        ...
