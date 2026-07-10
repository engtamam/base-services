"""Null provider — fails closed. No provider configured ⇒ never fabricate a connection."""
from __future__ import annotations

from app.ports.connector_provider import Page, ProviderUnavailable, Tokens

_MSG = "no connector provider configured (set CONNECTOR_PROVIDER_DRIVER)"


class NullConnector:
    driver = "null"

    def exchange(self, **kwargs) -> Tokens:
        raise ProviderUnavailable(_MSG)

    def refresh(self, **kwargs) -> Tokens:
        raise ProviderUnavailable(_MSG)

    def fetch(self, **kwargs) -> Page:
        raise ProviderUnavailable(_MSG)

    def push(self, **kwargs) -> dict:
        raise ProviderUnavailable(_MSG)

    def revoke(self, **kwargs) -> bool:
        return False
