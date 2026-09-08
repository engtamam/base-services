"""Null order-status backend — safe default: reports 'not available'."""
from __future__ import annotations

from typing import Any


class NullOrderStatusBackend:
    driver = "null"

    async def status(self, *, tenant: str, order: str) -> dict[str, Any]:
        return {"headline": "Status unavailable", "body": "Order tracking is not configured."}
