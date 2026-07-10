"""Orchestrator port — fan a data-subject task out to the services that hold the data.

A subject's personal data lives across services (identity, billing, file, …). An erasure or
export must reach EVERY one of them. This port dispatches one task per registered target;
each target later acknowledges completion. `null` records the tasks without dispatching
(useful in tests and single-service deployments) — it never claims the work was done.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Orchestrator(Protocol):
    driver: str

    async def dispatch(self, *, request_id: str, action: str, tenant: str, subject: str,
                       target: dict[str, Any]) -> bool:
        """Send one erasure/export task to one target. Returns True if it was dispatched."""
        ...
