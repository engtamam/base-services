"""Null reschedule backend — safe default: no appointments, refuses cancel/reschedule."""
from __future__ import annotations

from typing import Any


class NullReschedulingBackend:
    driver = "null"

    async def appointments(self, *, tenant: str, customer: str) -> list[dict]:
        return []

    async def calendar(self, *, tenant: str, appointment: str) -> dict[str, Any]:
        return {}

    async def slots(self, *, tenant: str, appointment: str, date: str) -> list[dict]:
        return []

    async def cancel(self, *, tenant: str, appointment: str, idempotency_key: str) -> dict[str, Any]:
        return {"ok": False, "reason": "Rescheduling is not configured."}

    async def reschedule(self, *, tenant: str, appointment: str, slot: str,
                         idempotency_key: str) -> dict[str, Any]:
        return {"ok": False, "reason": "Rescheduling is not configured."}
