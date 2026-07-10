"""Env SecretsProvider — reads from process environment. Zero infra."""
from __future__ import annotations

import os

from app.ports.errors import ConfigError


class EnvSecrets:
    driver = "env"

    def get(self, name: str) -> str | None:
        val = os.environ.get(name)
        return val if val else None

    def require(self, name: str) -> str:
        val = self.get(name)
        if val is None:
            raise ConfigError(f"required secret not set: {name}")
        return val
