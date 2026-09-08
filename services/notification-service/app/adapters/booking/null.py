"""Null booking backend — the safe default when no provider URL is configured.

Returns no options/dates/slots and refuses to reserve, so a deployment that enables the Flow endpoint
without wiring its booking service gets a clean "not available" rather than a broken or fabricated
booking. Implements both ports (AvailabilityProvider + BookingProvider)."""
from __future__ import annotations

from typing import Any


class NullBookingBackend:
    driver = "null"

    async def options(self, *, tenant: str, dimension: str, chosen: dict[str, Any]) -> list[dict]:
        return []

    async def calendar(self, *, tenant: str, chosen: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def slots(self, *, tenant: str, chosen: dict[str, Any], date: str) -> list[dict]:
        return []

    async def reserve(self, *, tenant: str, selection: dict[str, Any],
                      idempotency_key: str) -> dict[str, Any]:
        return {"ok": False, "reason": "Booking is not configured."}
