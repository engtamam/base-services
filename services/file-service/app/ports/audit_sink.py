"""AuditSink port — record that a privileged action happened, somewhere it cannot be edited.

audit-service keeps a hash-chained log: each event's hash seals the previous head, so a later edit
breaks `verify()` from that point on. Tamper-evidence IS the product. This port is how every other
service puts something INTO it.

It exists because the chain was empty. audit-service shipped a complete, token-guarded ingest
endpoint and not one of the other seventeen services called it — no config knob, no client, no
event-bus topic. A verifiable chain of zero events is worse than no chain at all: it passes a
compliance questionnaire and answers nothing in an investigation.

Three outcomes, and the split matters as much as it did for erasure:

    accepted  the log holds it, and the receipt carries the seq and hash that prove where.
    dropped   deliberately not delivered (the `null` sink) and SAYS so. Never dressed as success.
    failed    the sink could not reach the log. The event is NOT recorded, and the caller has to
              decide what that means — see the failure policy, which is a product decision rather
              than a transport one.

`emit` never raises. An audit sink that threw would let a logging hiccup fail the very action it
was trying to witness, and that turns a security control into an outage. It reports; the policy
above it decides whether the action may proceed unwitnessed.

WHAT IS AUDITABLE IS DECLARED, NOT INFERRED. Each service names its auditable actions explicitly,
because the alternative is exactly how the chain got empty in the first place: what nobody declares,
nobody logs, and nothing fails while it is missing.

THE ACTOR IS A PSEUDONYM. `actor` carries a `subject_ref`, never a name or an email. An audit log
full of raw identities becomes a PII store in its own right — and one that cannot honour an erasure,
because removing a row from a hash chain is precisely what breaks it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ACCEPTED = "accepted"
DROPPED = "dropped"
FAILED = "failed"


@dataclass(frozen=True)
class AuditEvent:
    """One thing that happened. Mirrors audit-service's ingest contract exactly."""

    service: str                                   # who is reporting
    action: str                                    # what happened, e.g. "user.suspended"
    actor: str = ""                                # subject_ref of who did it — never a name
    resource: str = ""                             # what it happened to
    tenant: str = ""
    ip: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"service": self.service, "action": self.action, "actor": self.actor,
                "resource": self.resource, "tenant": self.tenant, "ip": self.ip,
                "metadata": self.metadata}


@dataclass(frozen=True)
class AuditReceipt:
    status: str                                    # ACCEPTED | DROPPED | FAILED
    id: str = ""                                   # the log's own id for this event
    seq: int = 0                                   # position in the chain
    hash: str = ""                                 # the seal — proof of WHERE in the chain it sits
    detail: str = ""                               # why it was dropped or failed

    @property
    def is_recorded(self) -> bool:
        """True only when the log actually holds it. `dropped` is not recorded, and reading it as
        such is the mistake this property exists to prevent."""
        return self.status == ACCEPTED


@runtime_checkable
class AuditSink(Protocol):
    driver: str

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        """Record one event. MUST NOT raise — a sink that threw would let a logging problem fail
        the action it was meant to witness. Report FAILED instead and let the policy decide."""
        ...
