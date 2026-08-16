"""PrivacyTransport port — deliver ONE erasure request to ONE target and read its answer.

The target may be ours or anyone's: a service in any language on any platform that implements
docs/contracts/privacy-erase.schema.json. This port is deliberately dumb — it does not know which
targets exist (that is the data map), where they live (that is config), or when to retry (that is
the dispatcher). It carries one request and reports one of exactly three outcomes:

    acked    HTTP 200 with an ack body AND the work is finished — the body is the evidence.
    partial  HTTP 200 with an ack that says `truncated` — the target erased only SOME of the
             subject's rows this call and more remain. NOT done: the target stays UNACKED and the
             erase re-dispatches (idempotently) until a call returns without `truncated`. A target
             that erased 5,000 of 40,000 and said so must NEVER be recorded as a full acknowledgement.
    pending  HTTP 202 — the target erases asynchronously and will PUSH its ack later.
    failed   anything else — retried with backoff; after max attempts the target is marked FAILED
             and the DSAR stays incomplete and VISIBLE.

There is no "assumed done": a transport that cannot reach a target must say `failed`, never `acked`,
and one that only half-finished must say `partial`, never `acked`. A false "she was erased" — whether
from an unreachable target or a truncated one — is the worst lie this system can tell.

THE `subject` SHAPE IS FIXED, AND IT IS PASSED VERBATIM. `subject` is the platform's subject
reference for this tenant — the exact value the records key on. It travels on the wire UNCHANGED and
is NEVER re-hashed on either side: we hashed an already-hashed subject once, the target matched zero
rows, and it acknowledged success — a clean "erased" over a wrong id and nothing deleted. A target
pseudonymises for its ACK (see subject_ref in the schema), never to MATCH. An empty subject is refused
(FAILED/INVALID_SUBJECT), and a zero-row ack (already_absent) is recorded DISTINCTLY, so "matched
nothing" is never silently a clean full erasure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ACKED = "acked"
PARTIAL = "partial"
PENDING = "pending"
FAILED = "failed"


@dataclass(frozen=True)
class EraseOutcome:
    status: str                                   # ACKED | PARTIAL | PENDING | FAILED
    ack: dict[str, Any] | None = None             # the target's ack body, when status is ACKED/PARTIAL
    detail: str = ""                              # why it failed / stayed pending / is partial — for the record

    @property
    def is_acked(self) -> bool:
        # PARTIAL is deliberately NOT acked — completion is derived from acks, so a truncated erasure
        # must not count, or the DSAR closes over work that did not finish.
        return self.status == ACKED

    @property
    def is_partial(self) -> bool:
        return self.status == PARTIAL

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING


@runtime_checkable
class PrivacyTransport(Protocol):
    driver: str

    async def erase(self, *, url: str, token: str, request_id: str, action: str,
                    tenant: str, subject: str, timeout: int) -> EraseOutcome:
        """POST the erase request to `url` and classify the answer. Never raises for a remote
        failure — it returns FAILED with the detail, so the dispatcher can retry and record."""
        ...
