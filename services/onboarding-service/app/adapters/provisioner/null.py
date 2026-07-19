"""Null provisioner — creates nothing and says so.

Production refuses to boot on it when a flow declares provisioning, because a platform that
believes it provisions tenants and provisions none has a signup that appears to work and produces
nothing. Here for tests and for platforms whose flows only evaluate and grant.
"""
from __future__ import annotations

from app.ports.provisioner import FAILED, ProvisionRequest, ProvisionResult


class NullProvisioner:
    driver = "null"

    def __init__(self) -> None:
        self.created: list[ProvisionRequest] = []
        self.undone: list[str] = []

    async def create(self, request: ProvisionRequest) -> ProvisionResult:
        self.created.append(request)
        return ProvisionResult(status=FAILED,
                               detail="null provisioner: nothing was created (by design)")

    async def undo(self, request: ProvisionRequest, reference: str) -> ProvisionResult:
        # Undoing nothing is success — a compensation must not fail because the create never
        # happened, or a resumed rollback would loop on the one step that was never taken.
        self.undone.append(reference)
        return ProvisionResult(status="created", detail="nothing to undo")
