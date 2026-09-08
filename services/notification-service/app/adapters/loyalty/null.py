"""Null loyalty backend — safe default: no rewards, refuses redemption."""
from __future__ import annotations

from typing import Any


class NullLoyaltyBackend:
    driver = "null"

    async def balance(self, *, tenant: str, customer: str) -> dict[str, Any]:
        return {"points": 0}

    async def rewards(self, *, tenant: str, customer: str) -> list[dict]:
        return []

    async def redeem(self, *, tenant: str, customer: str, reward: str,
                     idempotency_key: str) -> dict[str, Any]:
        return {"ok": False, "reason": "Loyalty is not configured."}
