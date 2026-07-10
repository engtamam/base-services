"""SecretsProvider port — load secrets from env/file/vault/cloud."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsProvider(Protocol):
    driver: str

    def get(self, name: str) -> str | None:
        """Return the secret value, or None if absent. Never logs the value."""
        ...

    def require(self, name: str) -> str:
        """Return the secret value, or raise ConfigError if absent."""
        ...
