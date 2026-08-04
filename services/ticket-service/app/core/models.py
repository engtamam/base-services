"""Domain model — Ticket and the append-only TicketEvent log.

HISTORY IS THE TRUTH. Every change to a ticket is an immutable `TicketEvent` (created, replied,
status_changed, assigned, tagged, rated, ai_suggested, erased). The `Ticket` row is a PROJECTION of
that log — its current status/assignee/updated are a cache the log can rebuild. "Who did what and
when" is therefore a read of the log, never a reconstruction, and no reply can silently vanish.

PII IS PSEUDONYMISED. A ticket carries `raiser_ref`, not a person: an HMAC pseudonym (per tenant,
PII_SALT). It is stable enough to scope "my tickets" and to attribute the log, and erasable — the
same T2 pattern audit and compliance use. Free-text (subject, body) is the mutable PII an erasure
redacts; the pseudonyms and the ticket's shape survive so a resolved dispute stays provable.
"""
from __future__ import annotations

import datetime as _dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


_last_seq = 0


def seq_now() -> int:
    """A STRICTLY increasing ordering key for the append-only log.

    Two events appended in the same clock tick (Windows wall-clock resolution is coarse — ~15ms)
    would otherwise share a timestamp and tie-break on a random id, scrambling 'who did what and
    when'. We seed from wall-clock nanoseconds so order survives a restart roughly, but force each
    value strictly above the last so events in one process never collide."""
    global _last_seq
    v = time.time_ns()
    if v <= _last_seq:
        v = _last_seq + 1
    _last_seq = v
    return v

# The kinds of thing that can happen to a ticket. The log is closed over this set so a typo cannot
# invent an event kind that no reader knows how to render.
EVENT_KINDS = (
    "created", "replied", "status_changed", "assigned", "tagged", "rated", "ai_suggested", "erased",
)

PRIORITIES = ("low", "normal", "high", "urgent")


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Ticket:
    id: str
    tenant: str
    type: str
    subject: str = ""
    status: str = ""
    raiser_ref: str = ""                 # HMAC pseudonym of who raised it — never a raw identity
    assignee: str = ""                   # an operator/member id, or "" for unassigned
    priority: str = "normal"
    ref: dict[str, Any] = field(default_factory=dict)   # {"kind": "...", "id": "..."} — opaque here
    tags: list[str] = field(default_factory=list)
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tenant": self.tenant, "type": self.type, "subject": self.subject,
            "status": self.status, "raiser_ref": self.raiser_ref, "assignee": self.assignee,
            "priority": self.priority, "ref": self.ref, "tags": self.tags,
            "created": self.created, "updated": self.updated, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ticket":
        return cls(
            id=d["id"], tenant=d.get("tenant", ""), type=d.get("type", ""),
            subject=d.get("subject", ""), status=d.get("status", ""),
            raiser_ref=d.get("raiser_ref", ""), assignee=d.get("assignee", ""),
            priority=d.get("priority", "normal"), ref=dict(d.get("ref") or {}),
            tags=list(d.get("tags") or []), created=d.get("created", now()),
            updated=d.get("updated", now()), meta=dict(d.get("meta") or {}))

    def public(self) -> dict[str, Any]:
        return self.to_dict()

    def touch(self) -> None:
        self.updated = now()


@dataclass
class TicketEvent:
    id: str
    tenant: str
    ticket_id: str
    kind: str                            # one of EVENT_KINDS
    actor_ref: str = ""                  # pseudonym of who acted (raiser or operator)
    body: str = ""                       # free text for a reply; redacted on erasure
    data: dict[str, Any] = field(default_factory=dict)   # e.g. {"from": "new", "to": "triaged"}
    created: str = field(default_factory=now)
    seq: int = field(default_factory=seq_now)   # monotonic ordering key — the log's true order

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tenant": self.tenant, "ticket_id": self.ticket_id, "kind": self.kind,
            "actor_ref": self.actor_ref, "body": self.body, "data": self.data,
            "created": self.created, "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TicketEvent":
        return cls(
            id=d["id"], tenant=d.get("tenant", ""), ticket_id=d.get("ticket_id", ""),
            kind=d.get("kind", ""), actor_ref=d.get("actor_ref", ""), body=d.get("body", ""),
            data=dict(d.get("data") or {}), created=d.get("created", now()),
            seq=int(d.get("seq", 0)))

    def public(self) -> dict[str, Any]:
        return self.to_dict()
