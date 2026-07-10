"""Null flag provider — knows no flags, so every flag falls back to its default (OFF)."""
from __future__ import annotations

from typing import Any


class NullFlags:
    driver = "null"

    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def list(self, limit: int = 500) -> list[dict[str, Any]]:
        return []
