"""PrivacyTransport port — deliver ONE erasure request to ONE target and read its answer.

The target may be ours or anyone's: a service in any language on any platform that implements
docs/contracts/privacy-erase.schema.json. This port is deliberately dumb — it does not know which
targets exist (that is the data map), where they live (that is config), or when to retry (that is
the dispatcher). It carries one request and reports one of exactly three outcomes:

    acked    HTTP 200 with an ack body — the work is DONE and the body is the evidence.
    pending  HTTP 202 — the target erases asynchronously and will PUSH its ack later.
    failed   anything else — retried with backoff; after max attempts the target is marked FAILED
             and the DSAR stays incomplete and VISIBLE.

There is no fourth outcome, and in particular there is no "assumed done": a transport that cannot
reach a target must say `failed`, never `acked`. A false "she was erased" is the worst lie this
system can tell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ACKED = "acked"
PENDING = "pending"
FAILED = "failed"


@dataclass(frozen=True)
class EraseOutcome:
    status: str                                   # ACKED | PENDING | FAILED
    ack: dict[str, Any] | None = None             # the target's ack body, when status is ACKED
    detail: str = ""                              # why it failed / stayed pending — for the record

    @property
    def is_acked(self) -> bool:
        return self.status == ACKED

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
