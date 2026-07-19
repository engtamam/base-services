"""Provisioner port — create (and un-create) one thing in another service.

Onboarding orchestrates: it asks tenant-service for a tenant, identity-service for a membership,
wallet-service for a credit. It owns none of them. What it owns is the ORDER, and the obligation to
put the world back if a later step fails.

That obligation is why `undo` sits on the port rather than being left to each caller. A create
without a matching delete is how Kobo ends up with orphan tenants: provisioning succeeds, owner
registration fails, and nothing un-provisions because nobody wrote the undo at the time they wrote
the create. Pairing them in the interface makes forgetting it a visible omission rather than an
invisible one.

Three rules:

**Idempotent creates.** Same provisioning key, same result — a retry after a timeout must not make
a second tenant. The key is ours and stable, so the downstream service can dedupe on it.

**Idempotent undos.** Compensation can fail partway and be resumed, and a resumed run re-attempts
undos that already succeeded. Deleting something already deleted is success, not an error.

**Neither raises for a remote failure.** A create returns failure; the Saga decides. An adapter
that threw would skip the compensation that the failure exists to trigger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

CREATED = "created"
EXISTS = "exists"          # idempotent replay — already ours, not a second one
FAILED = "failed"


@dataclass(frozen=True)
class ProvisionRequest:
    kind: str                      # "tenant", "membership", …
    tenant: str
    subject: str
    # OUR stable key for this provisioning attempt. The downstream service dedupes on it, which is
    # what makes a retry safe across a restart rather than only within one process.
    key: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProvisionResult:
    status: str
    reference: str = ""            # the created thing's id, and what undo() is given
    detail: str = ""

    @property
    def ok(self) -> bool:
        """EXISTS counts as ok: from the caller's side the thing is there, and treating a replay as
        failure would trigger a compensation that deletes something legitimately created."""
        return self.status in (CREATED, EXISTS)


@runtime_checkable
class Provisioner(Protocol):
    driver: str

    async def create(self, request: ProvisionRequest) -> ProvisionResult:
        """Make it. MUST be idempotent on request.key and MUST NOT raise."""
        ...

    async def undo(self, request: ProvisionRequest, reference: str) -> ProvisionResult:
        """Un-make it. MUST be idempotent — already gone is success. MUST NOT raise."""
        ...
