"""File SecretsProvider — reads a KEY=VALUE file. Zero infra.

Fail-open + env-wins, mirroring (and generalizing) ammar's Vault seam: the
process environment always overrides the file, so an operator can override any
file secret without editing the file. Missing/unreadable file => no secrets,
never a crash.
"""
from __future__ import annotations

import os

from app.ports.errors import ConfigError


def _parse(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        # fail-open: no file -> no secrets
        return {}
    return data


class FileSecrets:
    driver = "file"

    def __init__(self, path: str) -> None:
        self._file = _parse(path) if path else {}

    def get(self, name: str) -> str | None:
        # env always wins over the file (opt-in override, never silent).
        env_val = os.environ.get(name)
        if env_val:
            return env_val
        val = self._file.get(name)
        return val if val else None

    def require(self, name: str) -> str:
        val = self.get(name)
        if val is None:
            raise ConfigError(f"required secret not set: {name}")
        return val
