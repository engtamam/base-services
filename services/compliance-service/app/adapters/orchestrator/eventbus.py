"""EventBus orchestrator — publish one task per target; services subscribe and ack."""
from __future__ import annotations

from typing import Any


class EventBusOrchestrator:
    driver = "eventbus"

    def __init__(self, bus) -> None:
        self._bus = bus

    async def dispatch(self, *, request_id: str, action: str, tenant: str, subject: str,
                       target: dict[str, Any]) -> bool:
        try:
            await self._bus.publish(f"compliance.{action}", {
                "request_id": request_id, "tenant": tenant, "subject": subject,
                "service": target.get("service"), "category": target.get("category")})
            return True
        except Exception:
            return False        # not dispatched → the request stays incomplete
