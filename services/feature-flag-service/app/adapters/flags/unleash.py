"""Unleash provider — read definitions from an Unleash server (stdlib HTTP).

Maps Unleash's shape onto ours: `name`→key, `enabled`, and a `flexibleRollout`
strategy's `rollout` parameter → our `rollout_pct`. Our engine still evaluates.
Unreachable → ProviderUnavailable (callers fall back to the flag default).
"""
from __future__ import annotations

from typing import Any

from app.adapters.flag_http import get_json
from app.ports.flag_provider import ProviderUnavailable


class UnleashFlags:
    driver = "unleash"

    def __init__(self, base_url: str, token: str) -> None:
        if not (base_url and token):
            raise ProviderUnavailable("UNLEASH_URL + UNLEASH_TOKEN required")
        self._base = base_url.rstrip("/")
        self._h = {"Authorization": token}

    def _map(self, f: dict) -> dict[str, Any]:
        pct = 100
        for s in f.get("strategies", []):
            if s.get("name") == "flexibleRollout":
                pct = int((s.get("parameters") or {}).get("rollout", 100))
        return {"key": f.get("name", ""), "enabled": bool(f.get("enabled", False)),
                "rollout_pct": pct, "default": False,
                "description": f.get("description", "")}

    async def get(self, key: str) -> dict[str, Any] | None:
        code, resp = get_json(f"{self._base}/api/client/features/{key}", self._h)
        if code == 0:
            raise ProviderUnavailable("Unleash unreachable")
        return self._map(resp) if 200 <= code < 300 and resp.get("name") else None

    async def list(self, limit: int = 500) -> list[dict[str, Any]]:
        code, resp = get_json(f"{self._base}/api/client/features", self._h)
        if code == 0:
            raise ProviderUnavailable("Unleash unreachable")
        return [self._map(f) for f in (resp.get("features") or [])][:limit]
