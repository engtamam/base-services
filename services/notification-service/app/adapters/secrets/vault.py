"""HashiCorp Vault SecretsProvider — infra-gated, lazy import. Fail-open, env-wins."""
from __future__ import annotations

import os
from typing import Any

from app.adapters.lazy import load
from app.ports.errors import ConfigError


class VaultSecrets:
    driver = "vault"

    def __init__(self, addr: str, token: str, path: str) -> None:
        if not (addr and token and path):
            raise ConfigError("VAULT_ADDR, VAULT_TOKEN and VAULT_PATH are required")
        hvac = load("hvac", "vault")
        self._data: dict[str, str] = {}
        try:
            client: Any = hvac.Client(url=addr, token=token)
            resp = client.secrets.kv.v2.read_secret_version(path=path)
            self._data = {str(k): str(v) for k, v in resp["data"]["data"].items()}
        except Exception:
            # fail-open: unreachable/sealed Vault -> no secrets, never blocks boot.
            self._data = {}

    def get(self, name: str) -> str | None:
        env_val = os.environ.get(name)  # env always wins
        if env_val:
            return env_val
        val = self._data.get(name)
        return val if val else None

    def require(self, name: str) -> str:
        val = self.get(name)
        if val is None:
            raise ConfigError(f"required secret not set: {name}")
        return val
