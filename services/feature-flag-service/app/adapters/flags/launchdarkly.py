"""LaunchDarkly provider — lazy import, optional [ld] extra."""
from __future__ import annotations

import asyncio
from typing import Any

from app.adapters.lazy import load
from app.ports.flag_provider import ProviderUnavailable


class LaunchDarklyFlags:
    driver = "launchdarkly"

    def __init__(self, sdk_key: str) -> None:
        if not sdk_key:
            raise ProviderUnavailable("LAUNCHDARKLY_SDK_KEY required")
        ldclient = load("ldclient", "ld")
        config_mod = load("ldclient.config", "ld")
        ldclient.set_config(config_mod.Config(sdk_key))
        self._client = ldclient.get()

    def _map(self, key: str, enabled: bool) -> dict[str, Any]:
        return {"key": key, "enabled": bool(enabled), "rollout_pct": 100, "default": False}

    def _all_flags(self) -> dict:
        # ldclient.all_flags_state is a synchronous SDK call — kept in one place so both get()/list()
        # offload it off the event loop (feature-flag-004).
        return self._client.all_flags_state({"key": "anonymous"}).to_values_map()

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            all_flags = await asyncio.to_thread(self._all_flags)
        except Exception as exc:
            raise ProviderUnavailable("LaunchDarkly unreachable") from exc
        return self._map(key, all_flags[key]) if key in all_flags else None

    async def list(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            all_flags = await asyncio.to_thread(self._all_flags)
        except Exception as exc:
            raise ProviderUnavailable("LaunchDarkly unreachable") from exc
        return [self._map(k, v) for k, v in list(all_flags.items())[:limit]]
