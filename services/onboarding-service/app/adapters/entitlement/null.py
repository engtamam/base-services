"""Null entitlement — grants nothing, and never claims otherwise.

The boot guard refuses this in production for any flow that declares a grant, because a platform
that promises a welcome credit and issues none has told every customer something untrue. Here for
tests and for a platform whose flows grant nothing at all.
"""
from __future__ import annotations

from app.ports.entitlement import FAILED, GrantRequest, Receipt


class NullEntitlement:
    driver = "null"

    def __init__(self) -> None:
        self.requests: list[GrantRequest] = []

    async def issue(self, request: GrantRequest) -> Receipt:
        self.requests.append(request)
        return Receipt(status=FAILED,
                       detail="null entitlement: nothing was granted (by design). Configure "
                              "ONBOARDING_ENTITLEMENT to actually issue grants.")
