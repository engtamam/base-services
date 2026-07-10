"""Null Fatoora client — always unreachable (invoice stays pending, retryable)."""
from __future__ import annotations

from typing import Any

from app.ports.fatoora_client import FatooraUnavailable

_MSG = "Fatoora not configured (set FATOORA_CLIENT_DRIVER=sidecar|direct); invoice stays pending"


class NullFatoora:
    driver = "null"

    def report(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable(_MSG)

    def clear(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable(_MSG)

    def onboard_compliance(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable(_MSG)

    def onboard_production(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise FatooraUnavailable(_MSG)
